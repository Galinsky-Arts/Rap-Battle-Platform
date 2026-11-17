"""Checkout (tickets, PPV, tips, memberships, paid media, sponsor invoices) and the Stripe webhook."""
from .http import HttpError, integer, money, json_response
from .views import M, h, post_button
from ..web.db import audit
from .. import payments

MEMBERSHIP_CENTS = 500


def mount(r, app):
    db, eng = app.db, app.engine

    def require_payments():
        if not app.stripe.enabled:
            raise HttpError(409, "Online payments are switched off. A platform admin enables them by setting STRIPE_SECRET_KEY and STRIPE_WEBHOOK_SECRET.")

    def checkout(ctx, order, name):
        base = app.origin
        session = app.stripe.checkout(order_id=order["id"], name=name, amount_cents=order["amount_cents"], currency=order["currency"],
                                      success_url=f"{base}/pay/success?order={order['id']}", cancel_url=f"{base}/pay/cancel?order={order['id']}",
                                      email=ctx.user.email if ctx.user else None)
        url = session.get("url")
        if not (isinstance(url, str) and url.startswith("https://checkout.stripe.com/")):
            raise HttpError(502, "Payment provider returned no checkout page")
        db.run("UPDATE orders SET stripe_session_id=%s WHERE id=%s", (session.get("id"), order["id"]))
        return ctx.redirect(url)

    @r.post("/pay/checkout")
    def pay(ctx):
        require_payments()
        u = ctx.require()
        if not app.limiter.allow("checkout:" + str(u.id), 20, 3600):
            raise HttpError(429, "Too many checkout attempts; try later")
        kind = ctx.form.get("kind")
        if kind in ("ticket", "ppv"):
            e = eng.event(integer(ctx.form.get("event_id"), lo=1))
            cents = e["ticket_cents"] if kind == "ticket" else e["ppv_cents"]
            if not e["published"] or not cents or e["status"] in ("completed", "cancelled"):
                raise HttpError(409, "This event is not selling that")
            o = payments.create_order(db, league=e["league_slug"], kind=kind, amount_cents=cents, buyer_id=u.id, event_id=e["id"])
            name = f"{payments.KIND_LABEL[kind]}: {e['title']}"
        elif kind in ("tip", "membership"):
            a = db.one("SELECT * FROM artists WHERE id=%s AND status='active' AND consent_profile", (integer(ctx.form.get("artist_id"), lo=1),))
            if not a or not a["payout_consent"]:
                raise HttpError(409, "This emcee is not accepting support")
            if a["user_id"] == u.id:
                raise HttpError(400, "You cannot pay yourself")
            cents = money(ctx.form.get("amount"), name="Tip", allow_zero=False) if kind == "tip" else MEMBERSHIP_CENTS
            if cents > 50000:
                raise HttpError(400, "Tips are limited to $500")
            o = payments.create_order(db, league=a["league_slug"], kind=kind, amount_cents=cents, buyer_id=u.id, artist_id=a["id"])
            name = f"{payments.KIND_LABEL[kind]} for {a['stage_name']}"
        elif kind == "paid_media":
            m = db.one("SELECT pm.*, a.league_slug, a.payout_consent, a.stage_name FROM paid_media pm JOIN artists a ON a.id=pm.artist_id WHERE pm.id=%s AND pm.active", (integer(ctx.form.get("media_id"), lo=1),))
            if not m or not m["payout_consent"]:
                raise HttpError(409, "This item is not for sale")
            o = payments.create_order(db, league=m["league_slug"], kind="paid_media", amount_cents=m["price_cents"], buyer_id=u.id, artist_id=m["artist_id"], ref_id=m["id"])
            name = f"{m['title']} by {m['stage_name']}"
        else:
            raise HttpError(400, "Unknown purchase")
        return checkout(ctx, o, name)

    @r.get("/sponsor/invoices/{iid}")
    def invoice_page(ctx, iid):
        inv = db.one("SELECT i.*, s.name AS sponsor_name, l.name AS league_name FROM invoices i JOIN sponsors s ON s.id=i.sponsor_id JOIN leagues l ON l.slug=i.league_slug WHERE i.id=%s", (integer(iid, lo=1),))
        if not inv:
            raise HttpError(404, "Invoice not found")
        body = M(f"<h1>Invoice #{inv['id']}</h1><p>{h(inv['league_name'])} → {h(inv['sponsor_name'])}: {h(payments.fmt_money(inv['amount_cents']))} · status {h(inv['status'])}</p>")
        if inv["status"] == "open":
            body += post_button(f"/pay/invoice/{inv['id']}", "Pay by card", ctx.csrf) if app.stripe.enabled else M("<p class='muted'>Online payment is switched off; pay the league directly.</p>")
        return ctx.page("Invoice", body)

    @r.post("/pay/invoice/{iid}")
    def pay_invoice(ctx, iid):
        require_payments()
        inv = db.one("SELECT i.*, s.name AS sponsor_name FROM invoices i JOIN sponsors s ON s.id=i.sponsor_id WHERE i.id=%s AND i.status='open'", (integer(iid, lo=1),))
        if not inv:
            raise HttpError(404, "Invoice not found")
        o = payments.create_order(db, league=inv["league_slug"], kind="sponsor", amount_cents=inv["amount_cents"], buyer_id=ctx.user and ctx.user.id, ref_id=inv["id"])
        return checkout(ctx, o, f"Sponsorship invoice #{inv['id']} ({inv['sponsor_name']})")

    @r.get("/pay/success")
    def success(ctx):
        oid = integer(ctx.req.query.get("order"), lo=1, default=0)
        o = db.one("SELECT * FROM orders WHERE id=%s", (oid,)) if oid else None
        state = o["status"] if o else "unknown"
        msg = "Payment confirmed — thank you." if state == "paid" else "Thanks! Your payment is being confirmed; this page updates when the payment provider notifies us."
        return ctx.page("Payment", M(f"<h1>Payment</h1><p>{h(msg)}</p><p><a href='/account'>Your purchases</a></p>"), refresh=None if state == "paid" else 5)

    @r.get("/pay/cancel")
    def cancel(ctx):
        return ctx.page("Payment cancelled", M("<h1>Payment cancelled</h1><p>No charge was made.</p>"))

    @r.post("/stripe/webhook", raw=True)
    def webhook(ctx):
        if not app.stripe.enabled:
            return json_response(404, {"error": "payments disabled"})
        payload = ctx.req.read_body(512 * 1024)
        event = app.stripe.verify(payload, ctx.req.header("Stripe-Signature"))
        result = payments.handle_event(db, event)
        audit(db, None, "stripe." + str(event.get("type")), {"id": event.get("id"), "result": result})
        return json_response(200, {"received": True, "result": result})
