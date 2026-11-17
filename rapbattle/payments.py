"""RAP-06/10/11 money: Stripe Checkout (off until keys exist), orders, revenue splits, ledger,
refunds/chargebacks, settlements with evidence hashes. No keys are ever invented; without
STRIPE_SECRET_KEY and STRIPE_WEBHOOK_SECRET every purchase button explains how to enable payments."""
import hashlib
import hmac
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

from psycopg.types.json import Jsonb

from .engine.scoring import split
from .web.http import HttpError

KINDS = ("ticket", "ppv", "tip", "membership", "paid_media", "sponsor")
KIND_LABEL = {"ticket": "Ticket", "ppv": "Pay-per-view", "tip": "Tip", "membership": "Membership (30 days)", "paid_media": "Paid media", "sponsor": "Sponsorship invoice"}
# Default revenue splits in basis points (platform, league, artist, event pool). Configurable per league.
DEFAULT_SPLITS = {
    "ticket": (1000, 6000, 0, 3000), "ppv": (1000, 6000, 0, 3000), "tip": (1000, 0, 9000, 0),
    "membership": (1000, 1000, 8000, 0), "paid_media": (1000, 1000, 8000, 0), "sponsor": (1500, 8500, 0, 0),
}
MEMBERSHIP_DAYS = 30


class Stripe:
    """Tiny Stripe REST client (form-encoded) with webhook signature verification."""
    API = "https://api.stripe.com/v1/"

    def __init__(self, secret_key, webhook_secret, opener=None):
        self.secret_key, self.webhook_secret = secret_key or "", webhook_secret or ""
        self.opener = opener or urllib.request.urlopen

    @property
    def enabled(self):
        return self.secret_key.startswith(("sk_live_", "sk_test_", "rk_live_", "rk_test_")) and self.webhook_secret.startswith("whsec_")

    def _post(self, path, fields):
        data = urllib.parse.urlencode(fields).encode()
        req = urllib.request.Request(self.API + path, data=data, method="POST", headers={
            "Authorization": "Bearer " + self.secret_key, "Content-Type": "application/x-www-form-urlencoded", "Stripe-Version": "2024-06-20"})
        try:
            with self.opener(req, timeout=15) as res:
                return json.loads(res.read().decode())
        except urllib.error.HTTPError as e:
            try:
                msg = json.loads(e.read().decode()).get("error", {}).get("message", "")
            except Exception:
                msg = ""
            raise HttpError(502 if e.code >= 500 else 400, "Payment provider error: " + (msg or str(e.code)))
        except (urllib.error.URLError, TimeoutError):
            raise HttpError(503, "Payment provider unavailable; try again")

    def checkout(self, *, order_id, name, amount_cents, currency, success_url, cancel_url, email=None):
        fields = {
            "mode": "payment", "success_url": success_url, "cancel_url": cancel_url, "client_reference_id": str(order_id),
            "metadata[order_id]": str(order_id), "payment_intent_data[metadata][order_id]": str(order_id),
            "line_items[0][quantity]": "1", "line_items[0][price_data][currency]": currency,
            "line_items[0][price_data][unit_amount]": str(amount_cents), "line_items[0][price_data][product_data][name]": name[:120],
        }
        if email:
            fields["customer_email"] = email
        return self._post("checkout/sessions", fields)

    def refund(self, payment_intent, amount_cents=None):
        fields = {"payment_intent": payment_intent}
        if amount_cents:
            fields["amount"] = str(amount_cents)
        return self._post("refunds", fields)

    def verify(self, payload, header, now=None, tolerance=300):
        """Validates the Stripe-Signature header (v1 HMAC-SHA256 over 't.payload')."""
        if not self.webhook_secret or not header:
            raise HttpError(400, "Missing signature")
        parts = {}
        for item in header.split(","):
            k, _, v = item.partition("=")
            parts.setdefault(k.strip(), []).append(v.strip())
        try:
            ts = int(parts.get("t", ["0"])[0])
        except ValueError:
            raise HttpError(400, "Bad signature")
        expected = hmac.new(self.webhook_secret.encode(), f"{ts}.".encode() + payload, hashlib.sha256).hexdigest()
        if not any(hmac.compare_digest(expected, sig) for sig in parts.get("v1", [])):
            raise HttpError(400, "Bad signature")
        if abs((now or time.time()) - ts) > tolerance:
            raise HttpError(400, "Stale signature")
        return json.loads(payload.decode())


def split_rule(db, league, kind):
    r = db.one("SELECT * FROM split_rules WHERE league_slug=%s AND kind=%s", (league, kind))
    if r:
        return r
    p, l, a, e = DEFAULT_SPLITS[kind]
    return {"league_slug": league, "kind": kind, "platform_bps": p, "league_bps": l, "artist_bps": a, "event_pool_bps": e, "default": True}


def set_split_rule(db, league, kind, platform_bps, league_bps, artist_bps, event_pool_bps):
    if kind not in KINDS:
        raise HttpError(400, "Unknown revenue kind")
    vals = (platform_bps, league_bps, artist_bps, event_pool_bps)
    if any(v < 0 or v > 10000 for v in vals) or sum(vals) != 10000:
        raise HttpError(400, "Shares must add up to exactly 100%")
    if kind in ("tip", "membership", "paid_media") and artist_bps == 0:
        raise HttpError(400, "Artist-directed revenue must give the artist a share")
    db.run("""INSERT INTO split_rules(league_slug,kind,platform_bps,league_bps,artist_bps,event_pool_bps) VALUES (%s,%s,%s,%s,%s,%s)
        ON CONFLICT (league_slug,kind) DO UPDATE SET platform_bps=EXCLUDED.platform_bps, league_bps=EXCLUDED.league_bps,
        artist_bps=EXCLUDED.artist_bps, event_pool_bps=EXCLUDED.event_pool_bps""", (league, kind, *vals))


def create_order(db, *, league, kind, amount_cents, buyer_id=None, artist_id=None, event_id=None, ref_id=None):
    if kind not in KINDS:
        raise HttpError(400, "Unknown purchase")
    if amount_cents < 50:
        raise HttpError(400, "Amount must be at least 0.50")
    return db.one("""INSERT INTO orders(league_slug,kind,buyer_user_id,artist_id,event_id,ref_id,amount_cents) VALUES (%s,%s,%s,%s,%s,%s,%s) RETURNING *""",
                  (league, kind, buyer_id, artist_id, event_id, ref_id, amount_cents))


def _post_ledger(q, order, amount_cents, entry):
    """Writes sale split lines using the league's current rule. Totals always equal the amount."""
    rule = split_rule(q, order["league_slug"], order["kind"])
    parts = split(amount_cents, rule)
    sign = 1
    for party, cents in parts.items():
        if cents:
            q.run("INSERT INTO ledger(order_id,league_slug,party,artist_id,event_id,amount_cents,entry) VALUES (%s,%s,%s,%s,%s,%s,%s)",
                  (order["id"], order["league_slug"], party, order["artist_id"] if party == "artist" else None,
                   order["event_id"] if party == "event_pool" else None, sign * cents, entry))


def _post_reversal(q, order, amount_cents, entry):
    """Reverses `amount_cents` in the same proportions as the order's original sale lines."""
    sales = q.all("SELECT * FROM ledger WHERE order_id=%s AND entry='sale' ORDER BY id", (order["id"],))
    shares = [(ln, ln["amount_cents"] * amount_cents // order["amount_cents"]) for ln in sales]
    rest = amount_cents - sum(c for _, c in shares)
    if shares and rest:
        i = max(range(len(shares)), key=lambda k: shares[k][0]["amount_cents"])
        shares[i] = (shares[i][0], shares[i][1] + rest)
    for ln, cents in shares:
        if cents:
            q.run("INSERT INTO ledger(order_id,league_slug,party,artist_id,event_id,amount_cents,entry) VALUES (%s,%s,%s,%s,%s,%s,%s)",
                  (order["id"], order["league_slug"], ln["party"], ln["artist_id"], ln["event_id"], -cents, entry))


def mark_paid(db, order_id, *, payment_intent=None, session_id=None):
    """Idempotent: a paid order is never double-posted."""
    with db.tx() as q:
        o = q.one("SELECT * FROM orders WHERE id=%s FOR UPDATE", (order_id,))
        if not o:
            raise HttpError(404, "Order not found")
        if o["status"] != "pending":
            return o
        access = datetime.now(timezone.utc) + timedelta(days=MEMBERSHIP_DAYS) if o["kind"] == "membership" else None
        o = q.one("""UPDATE orders SET status='paid', paid_at=now(), stripe_payment_intent=COALESCE(%s,stripe_payment_intent),
            stripe_session_id=COALESCE(%s,stripe_session_id), access_until=%s WHERE id=%s RETURNING *""", (payment_intent, session_id, access, order_id))
        _post_ledger(q, o, o["amount_cents"], "sale")
        if o["kind"] == "sponsor" and o["ref_id"]:
            q.run("UPDATE invoices SET status='paid', paid_at=now(), order_id=%s WHERE id=%s", (o["id"], o["ref_id"]))
        return o


def reverse(db, order_id, amount_cents, entry):
    """Refund or chargeback: posts negative split lines for the reversed amount (capped at what remains)."""
    with db.tx() as q:
        o = q.one("SELECT * FROM orders WHERE id=%s FOR UPDATE", (order_id,))
        if not o or o["status"] not in ("paid", "partially_refunded", "disputed"):
            return None
        remaining = o["amount_cents"] - o["refunded_cents"]
        amount = min(amount_cents, remaining)
        if amount <= 0:
            return o
        _post_reversal(q, o, amount, entry)
        total = o["refunded_cents"] + amount
        status = "disputed" if entry == "chargeback" else ("refunded" if total >= o["amount_cents"] else "partially_refunded")
        return q.one("UPDATE orders SET refunded_cents=%s, status=%s WHERE id=%s RETURNING *", (total, status, order_id))


def handle_event(db, event):
    """Applies a verified Stripe event once (deduplicated by event id)."""
    eid, etype = event.get("id"), event.get("type")
    if not eid:
        raise HttpError(400, "Malformed event")
    try:
        db.run("INSERT INTO stripe_events(id,type) VALUES (%s,%s)", (eid, etype or ""))
    except Exception as e:  # duplicate delivery
        if "duplicate key" in str(e):
            return "duplicate"
        raise
    obj = (event.get("data") or {}).get("object") or {}
    if etype == "checkout.session.completed" and obj.get("payment_status") == "paid":
        order_id = int((obj.get("metadata") or {}).get("order_id") or obj.get("client_reference_id") or 0)
        o = db.one("SELECT * FROM orders WHERE id=%s", (order_id,))
        if not o or o["amount_cents"] != obj.get("amount_total"):
            return "ignored"
        mark_paid(db, order_id, payment_intent=obj.get("payment_intent"), session_id=obj.get("id"))
        return "paid"
    if etype == "checkout.session.expired":
        db.run("UPDATE orders SET status='expired' WHERE stripe_session_id=%s AND status='pending'", (obj.get("id"),))
        return "expired"
    if etype == "charge.refunded":
        o = db.one("SELECT * FROM orders WHERE stripe_payment_intent=%s", (obj.get("payment_intent"),))
        if o:
            delta = int(obj.get("amount_refunded") or 0) - o["refunded_cents"]
            if delta > 0:
                reverse(db, o["id"], delta, "refund")
            return "refunded"
    if etype == "charge.dispute.created":
        o = db.one("SELECT * FROM orders WHERE stripe_payment_intent=%s", (obj.get("payment_intent"),))
        if o:
            reverse(db, o["id"], int(obj.get("amount") or 0), "chargeback")
            return "chargeback"
    return "ignored"


def settle(db, league, actor_id):
    """Closes all unsettled ledger lines into a settlement with totals per party and a SHA-256 evidence hash.
    Event-pool lines are divided equally among that event's active emcees; artist lines whose artist has
    not given payout consent are reported as held."""
    with db.tx() as q:
        lines = q.all("SELECT * FROM ledger WHERE league_slug=%s AND settlement_id IS NULL ORDER BY id FOR UPDATE", (league,))
        if not lines:
            raise HttpError(409, "Nothing to settle")
        totals = {"platform": 0, "league": 0, "artists": {}, "held": {}, "event_pool_unallocated": 0}

        def credit(artist_id, cents):
            a = q.one("SELECT stage_name, payout_consent FROM artists WHERE id=%s", (artist_id,))
            bucket = "artists" if a and a["payout_consent"] else "held"
            key = f"{artist_id}:{a['stage_name'] if a else 'removed'}"
            totals[bucket][key] = totals[bucket].get(key, 0) + cents

        for ln in lines:
            if ln["party"] in ("platform", "league"):
                totals[ln["party"]] += ln["amount_cents"]
            elif ln["party"] == "artist":
                credit(ln["artist_id"], ln["amount_cents"])
            else:
                emcees = [r["artist_id"] for r in q.all("SELECT artist_id FROM entrants WHERE event_id=%s AND status='active' AND artist_id IS NOT NULL ORDER BY id", (ln["event_id"],))]
                if not emcees:
                    totals["event_pool_unallocated"] += ln["amount_cents"]
                    continue
                share, rest = divmod(ln["amount_cents"], len(emcees))
                for i, aid in enumerate(emcees):
                    credit(aid, share + (1 if i < rest else 0))
        evidence = hashlib.sha256(json.dumps([[ln["id"], ln["order_id"], ln["party"], ln["artist_id"], ln["event_id"], ln["amount_cents"], ln["entry"]] for ln in lines]).encode()).hexdigest()
        s = q.one("INSERT INTO settlements(league_slug,created_by,totals,evidence_sha256,entry_count) VALUES (%s,%s,%s,%s,%s) RETURNING *",
                  (league, actor_id, Jsonb(totals), evidence, len(lines)))
        q.run("UPDATE ledger SET settlement_id=%s WHERE id = ANY(%s)", (s["id"], [ln["id"] for ln in lines]))
        return s


def ledger_totals(db, league):
    return db.all("""SELECT party, entry, sum(amount_cents)::bigint AS cents, count(*)::int AS n FROM ledger WHERE league_slug=%s GROUP BY party, entry ORDER BY party, entry""", (league,))


def has_access(db, user_id, *, kind, event_id=None, artist_id=None, ref_id=None):
    if not user_id:
        return False
    cond, p = ["buyer_user_id=%s", "kind=%s", "status IN ('paid','partially_refunded')"], [user_id, kind]
    if event_id:
        cond.append("event_id=%s")
        p.append(event_id)
    if artist_id:
        cond.append("artist_id=%s")
        p.append(artist_id)
    if ref_id:
        cond.append("ref_id=%s")
        p.append(ref_id)
    if kind == "membership":
        cond.append("access_until > now()")
    return bool(db.one("SELECT 1 FROM orders WHERE " + " AND ".join(cond) + " LIMIT 1", p))


def fmt_money(cents, currency="usd"):
    sign = "-" if cents < 0 else ""
    return f"{sign}${abs(cents) / 100:,.2f}" if currency == "usd" else f"{sign}{abs(cents) / 100:,.2f} {currency.upper()}"
