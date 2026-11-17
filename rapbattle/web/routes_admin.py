"""Platform admin: leagues, users and roles, privacy/sign-up gate, voting and upload limits, audit log."""
import json
import re

from .http import HttpError, text, integer, one_of
from .views import M, field, textarea, select, csrf_field, table, post_button, h, fmt_dt
from .settings import privacy_gaps
from .security import norm_email
from ..web.db import audit
from ..store import Conflict


def purge(db, settings):
    """Retention: drop notifications, expired sessions and audit records older than the configured retention."""
    s = settings.all()
    db.run("DELETE FROM sessions WHERE expires_at < now()")
    if isinstance(s.get("retention_days"), int) and s["retention_days"] >= 1:
        n = db.run("DELETE FROM audit_log WHERE at < now() - make_interval(days => %s)", (s["retention_days"],))
        db.run("DELETE FROM notifications WHERE at < now() - make_interval(days => %s)", (s["retention_days"],))
        return n
    return 0


def mount(r, app):
    db = app.db

    @r.get("/admin")
    def admin_home(ctx):
        ctx.require_admin()
        leagues = db.all("SELECT l.*, (SELECT count(*) FROM events e WHERE e.league_slug=l.slug) AS events, (SELECT count(*) FROM artists a WHERE a.league_slug=l.slug) AS artists FROM leagues l ORDER BY l.name")
        s = app.settings.all()
        gaps = privacy_gaps(s)
        body = M(f"<h1>Platform admin</h1><p>Engine: {h(app.engine.kind)} · payments: {'ON' if app.stripe.enabled else 'OFF (set STRIPE_SECRET_KEY and STRIPE_WEBHOOK_SECRET)'} · artist sign-up: {'OPEN' if s['artist_signup_open'] else 'CLOSED'}{(' — missing ' + ', '.join(gaps)) if gaps else ''}</p>"
                 "<p class='row'><a href='/admin/settings'>Settings and privacy</a> <a href='/admin/users'>Users</a> <a href='/admin/audit'>Audit log</a></p>")
        body += table(["League", "Slug", "Events", "Emcees", ""], [[l["name"] + (" (synthetic)" if l["synthetic"] else ""), l["slug"], l["events"], l["artists"], M(f"<a href='/manage/{h(l['slug'])}'>Manage</a>")] for l in leagues])
        body += (M("<form method='post' action='/admin/leagues' class='card'><h2>New league</h2>") + csrf_field(ctx.csrf) + field("Name", "name", required=True) + field("Slug", "slug", required=True, hint="Lowercase letters, digits, hyphens.")
                 + field("Timezone", "timezone", value="America/New_York") + field("First promoter email (optional)", "producer_email") + M("<button type='submit'>Create league</button></form>"))
        return ctx.page("Admin", body)

    @r.post("/admin/leagues")
    def create_league(ctx):
        ctx.require_admin()
        slug = text(ctx.form.get("slug"), min_len=2, max_len=63, name="Slug")
        if not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", slug):
            raise HttpError(400, "Slug must be lowercase letters, digits and hyphens")
        try:
            app.store.add_league(slug, "org-" + slug, text(ctx.form.get("name"), min_len=2, max_len=120, name="Name"), text(ctx.form.get("timezone"), max_len=64) or "UTC")
        except (Conflict, ValueError) as e:
            raise HttpError(409 if isinstance(e, Conflict) else 400, "Could not create league: " + (str(e) or "slug already used"))
        except Exception as e:
            if "unique" in str(e).lower() or "duplicate" in str(e).lower():
                raise HttpError(409, "That league slug is already used")
            raise
        email = norm_email(ctx.form.get("producer_email"))
        if email:
            u = db.one("SELECT id FROM users WHERE email=%s", (email,))
            if u:
                db.run("INSERT INTO league_members(league_slug,user_id,role) VALUES (%s,%s,'producer') ON CONFLICT DO NOTHING", (slug, u["id"]))
        audit(db, ctx.user.id, "league.created", {"slug": slug}, slug)
        return ctx.redirect(f"/manage/{slug}", "League created.")

    @r.get("/admin/settings")
    def settings_page(ctx):
        ctx.require_admin()
        s = app.settings.all()
        gaps = privacy_gaps(s)
        body = M(f"<h1>Settings</h1><section class='card'><h2>Emcee sign-up is {'OPEN' if s['artist_signup_open'] else 'CLOSED'}</h2>")
        if gaps:
            body += M(f"<p class='notice'>Missing before sign-up can open: {h(', '.join(gaps))}.</p>")
        body += post_button("/admin/settings/signup", "Close emcee sign-up" if s["artist_signup_open"] else "Open emcee sign-up", ctx.csrf, fields={"open": "0" if s["artist_signup_open"] else "1"}, cls="danger" if s["artist_signup_open"] else "") + M("</section>")
        body += (M("<form method='post' action='/admin/settings' class='card'><h2>Privacy policy (owner decisions)</h2>") + csrf_field(ctx.csrf)
                 + field("Operator name", "operator_name", value=s["operator_name"]) + field("Privacy contact", "privacy_contact", value=s["privacy_contact"])
                 + field("Retention days (audit records, notifications)", "retention_days", type_="number", value=s["retention_days"] or "") + field("Policy version", "policy_version", value=s["policy_version"])
                 + textarea("Privacy policy text", "policy_text", value=s["policy_text"], rows=10)
                 + M("<h2>Voting and uploads</h2>") + field("Minimum account age to vote (minutes)", "vote_min_account_minutes", type_="number", value=s["vote_min_account_minutes"])
                 + field("Maximum votes per network per battle", "vote_ip_cap", type_="number", value=s["vote_ip_cap"]) + field("Photo upload cap (KB, stored in Postgres)", "upload_max_kb", type_="number", value=s["upload_max_kb"])
                 + M("<button type='submit'>Save settings</button></form>"))
        body += M("<section class='card'><h2>Payments</h2>" + ("<p>Stripe Checkout is enabled.</p>" if app.stripe.enabled else "<p>Payments are OFF. Set <code>STRIPE_SECRET_KEY</code> and <code>STRIPE_WEBHOOK_SECRET</code> in the host environment and point a Stripe webhook at <code>/stripe/webhook</code> (events: checkout.session.completed, checkout.session.expired, charge.refunded, charge.dispute.created).</p>") + "</section>")
        body += M("<section class='card'><h2>Retention</h2>") + post_button("/admin/retention", "Run retention purge now", ctx.csrf, cls="secondary") + M("</section>")
        return ctx.page("Settings", body)

    @r.post("/admin/settings")
    def save_settings(ctx):
        ctx.require_admin()
        f = ctx.form
        before = app.settings.all()
        values = {"operator_name": text(f.get("operator_name"), max_len=160), "privacy_contact": text(f.get("privacy_contact"), max_len=200),
                  "retention_days": integer(f.get("retention_days"), lo=1, hi=3650) if str(f.get("retention_days") or "").strip() else 0,
                  "policy_version": text(f.get("policy_version"), max_len=40), "policy_text": text(f.get("policy_text"), max_len=50000, name="Policy text"),
                  "vote_min_account_minutes": integer(f.get("vote_min_account_minutes"), lo=0, hi=10080), "vote_ip_cap": integer(f.get("vote_ip_cap"), lo=1, hi=10000),
                  "upload_max_kb": integer(f.get("upload_max_kb"), lo=32, hi=4096)}
        changed = any(before[k] != values[k] for k in ("operator_name", "privacy_contact", "retention_days", "policy_version", "policy_text"))
        if (changed and before["artist_signup_open"]) or privacy_gaps(values):
            values["artist_signup_open"] = False
        app.settings.update(values)
        audit(db, ctx.user.id, "settings.updated", {"policyChanged": changed})
        msg = "Saved. The privacy policy changed, so emcee sign-up was closed; re-open it to confirm." if changed and before["artist_signup_open"] else "Settings saved."
        return ctx.redirect("/admin/settings", msg)

    @r.post("/admin/settings/signup")
    def toggle_signup(ctx):
        ctx.require_admin()
        open_ = ctx.form.get("open") == "1"
        app.settings.set_signup_open(open_)
        audit(db, ctx.user.id, "signup.opened" if open_ else "signup.closed")
        return ctx.redirect("/admin/settings", "Emcee sign-up is open." if open_ else "Emcee sign-up is closed.")

    @r.post("/admin/retention")
    def retention(ctx):
        ctx.require_admin()
        n = purge(db, app.settings)
        return ctx.redirect("/admin/settings", f"Retention purge removed {n} audit record(s).")

    @r.get("/admin/users")
    def users(ctx):
        ctx.require_admin()
        q = text(ctx.req.query.get("q"), max_len=120)
        p = ["%" + q.replace("%", r"\%").replace("_", r"\_") + "%"] if q else []
        rows = db.all(f"""SELECT u.id, u.email, u.display_name, u.bootstrap, u.disabled, COALESCE(array_agg(DISTINCT r.role) FILTER (WHERE r.role IS NOT NULL), '{{}}') AS roles,
            COALESCE(array_agg(DISTINCT m.league_slug || ':' || m.role) FILTER (WHERE m.role IS NOT NULL), '{{}}') AS memberships
            FROM users u LEFT JOIN user_roles r ON r.user_id=u.id LEFT JOIN league_members m ON m.user_id=u.id {"WHERE u.email ILIKE %s OR u.display_name ILIKE %s" if q else ""}
            GROUP BY u.id ORDER BY u.id DESC LIMIT 200""", p * 2)
        leagues = db.all("SELECT slug, name FROM leagues ORDER BY name")
        body = M("<h1>Users</h1><form method='get' class='row'>") + field("Search", "q", value=q) + M("<button type='submit'>Search</button></form>")
        body += table(["Account", "Platform roles", "League roles", ""], [[M(f"{h(u['display_name'])}<br><span class='muted'>{h(u['email'])}{' (bootstrap)' if u['bootstrap'] else ''}{' (disabled)' if u['disabled'] else ''}</span>"), ", ".join(u["roles"]), ", ".join(u["memberships"]),
                                                                          (post_button(f"/admin/users/{u['id']}/admin", "Revoke admin" if "admin" in u["roles"] else "Make admin", ctx.csrf, fields={"on": "0" if "admin" in u["roles"] else "1"}, cls="secondary")
                                                                           + post_button(f"/admin/users/{u['id']}/disable", "Enable" if u["disabled"] else "Disable", ctx.csrf, fields={"on": "0" if u["disabled"] else "1"}, cls="secondary")) if not u["bootstrap"] else ""] for u in rows])
        body += (M("<form method='post' action='/admin/memberships' class='card row'><h2>Add league role</h2>") + csrf_field(ctx.csrf) + field("Account email", "email", required=True) + select("League", "league", [(l["slug"], l["name"]) for l in leagues])
                 + select("Role", "role", [("producer", "Promoter / producer"), ("judge", "Judge")]) + M("<button type='submit'>Add</button></form>"))
        return ctx.page("Users", body)

    @r.post("/admin/users/{uid}/admin")
    def set_admin(ctx, uid):
        ctx.require_admin()
        uid = integer(uid, lo=1)
        if uid == ctx.user.id:
            raise HttpError(400, "You cannot change your own admin role")
        if ctx.form.get("on") == "1":
            db.run("INSERT INTO user_roles(user_id,role) VALUES (%s,'admin') ON CONFLICT DO NOTHING", (uid,))
        else:
            db.run("DELETE FROM user_roles WHERE user_id=%s AND role='admin'", (uid,))
        audit(db, ctx.user.id, "role.admin", {"userId": uid, "on": ctx.form.get("on") == "1"})
        return ctx.redirect("/admin/users", "Role updated.")

    @r.post("/admin/users/{uid}/disable")
    def disable(ctx, uid):
        ctx.require_admin()
        uid = integer(uid, lo=1)
        if uid == ctx.user.id:
            raise HttpError(400, "You cannot disable yourself")
        on = ctx.form.get("on") == "1"
        db.run("UPDATE users SET disabled=%s WHERE id=%s AND NOT bootstrap", (on, uid))
        if on:
            app.sessions.destroy_all(uid)
        audit(db, ctx.user.id, "user.disabled" if on else "user.enabled", {"userId": uid})
        return ctx.redirect("/admin/users", "Account updated.")

    @r.post("/admin/memberships")
    def add_membership(ctx):
        ctx.require_admin()
        u = db.one("SELECT id FROM users WHERE email=%s", (norm_email(ctx.form.get("email")),))
        if not u:
            raise HttpError(400, "No account with that email")
        slug = ctx.form.get("league") or ""
        app.league(slug)
        role = one_of(ctx.form.get("role"), ("producer", "judge"), "role")
        db.run("INSERT INTO league_members(league_slug,user_id,role) VALUES (%s,%s,%s) ON CONFLICT DO NOTHING", (slug, u["id"], role))
        audit(db, ctx.user.id, "membership.added", {"userId": u["id"], "role": role}, slug)
        return ctx.redirect("/admin/users", "League role added.")

    @r.get("/admin/audit")
    def audit_page(ctx):
        ctx.require_admin()
        rows = db.all("SELECT a.*, u.email FROM audit_log a LEFT JOIN users u ON u.id=a.actor_user_id ORDER BY a.id DESC LIMIT 300")
        return ctx.page("Audit log", M("<h1>Audit log</h1>") + table(["When", "Who", "League", "Action", "Detail"], [[fmt_dt(a["at"]), a["email"] or "system", a["league_slug"] or "", a["action"], json.dumps(a["detail"])] for a in rows]))

