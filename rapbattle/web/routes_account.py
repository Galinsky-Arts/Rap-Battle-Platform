"""Accounts: email+password (argon2id), sessions, account page with artist profiles, contracts, purchases."""
import re

from .http import HttpError, text, integer, cookie, one_of
from .views import M, field, csrf_field, table, fmt_dt, post_button, h, join
from .security import hash_password, verify_password, password_problem, norm_email, valid_email, sha256
from ..web.db import audit, UniqueViolation
from .. import payments


def safe_next(n):
    return n if isinstance(n, str) and re.fullmatch(r"/(?![/\\])[\w\-/.?=&%]*", n) else "/account"


def mount(r, app):
    db = app.db

    def start_session(ctx, user_id):
        raw = app.sessions.create(user_id)
        ctx.set_cookies.append(cookie(app.sid, raw, secure=app.secure, max_age=14 * 86400))

    @r.get("/login")
    def login_form(ctx):
        if ctx.user:
            return ctx.redirect("/account")
        body = (M("<h1>Sign in</h1><form method='post' action='/login' class='card'>") + csrf_field(ctx.csrf) + M(f"<input type='hidden' name='next' value='{h(safe_next(ctx.req.query.get('next')))}'>")
                + field("Email", "email", required=True, attrs="autocomplete='username'") + field("Password", "password", type_="password", required=True, attrs="autocomplete='current-password'")
                + M("<button type='submit'>Sign in</button></form><p>New here? <a href='/register'>Create an account</a>.</p>"))
        return ctx.page("Sign in", body)

    @r.post("/login")
    def login(ctx):
        email, pw = norm_email(ctx.form.get("email")), str(ctx.form.get("password") or "")
        if not app.limiter.allow("login-ip:" + ctx.ip, 20, 900) or not app.limiter.allow("login-email:" + sha256(email), 10, 900):
            raise HttpError(429, "Too many sign-in attempts; try again in 15 minutes")
        u = db.one("SELECT * FROM users WHERE email=%s AND NOT disabled", (email,))
        if not verify_password(u and u["password_hash"], pw) or not u:
            audit(db, None, "login.failed", {"email": email[:3] + "…"})
            return ctx.redirect("/login", {"kind": "error", "text": "Email or password is incorrect."})
        start_session(ctx, u["id"])
        audit(db, u["id"], "login")
        return ctx.redirect(safe_next(ctx.form.get("next")), "Signed in.")

    @r.get("/register")
    def register_form(ctx):
        if ctx.user:
            return ctx.redirect("/account")
        body = (M("<h1>Create an account</h1><p>Fans can vote and support emcees; emcees can create a profile once artist sign-up is open.</p><form method='post' action='/register' class='card'>") + csrf_field(ctx.csrf)
                + field("Display name", "name", required=True, attrs="maxlength='80'") + field("Email", "email", type_="email", required=True)
                + field("Password", "password", type_="password", required=True, hint="At least 10 characters.", attrs="minlength='10' autocomplete='new-password'")
                + M("<button type='submit'>Create account</button></form><p>Read the <a href='/privacy'>privacy policy</a>.</p>"))
        return ctx.page("Create account", body)

    @r.post("/register")
    def register(ctx):
        if not app.limiter.allow("register:" + ctx.ip, 8, 3600):
            raise HttpError(429, "Too many new accounts from this network; try later")
        email, name, pw = norm_email(ctx.form.get("email")), text(ctx.form.get("name"), min_len=2, max_len=80, name="Display name"), str(ctx.form.get("password") or "")
        if not valid_email(email):
            raise HttpError(400, "Enter a valid email address")
        problem = password_problem(pw)
        if problem:
            raise HttpError(400, problem)
        try:
            u = db.one("INSERT INTO users(email,password_hash,display_name) VALUES (%s,%s,%s) RETURNING id", (email, hash_password(pw), name))
        except UniqueViolation:
            raise HttpError(409, "An account with that email already exists; sign in instead")
        db.run("INSERT INTO user_roles(user_id,role) VALUES (%s,'fan')", (u["id"],))
        audit(db, u["id"], "register")
        start_session(ctx, u["id"])
        return ctx.redirect("/account", "Welcome.")

    @r.post("/logout")
    def logout(ctx):
        app.sessions.destroy(ctx.req.cookies.get(app.sid))
        ctx.set_cookies.append(cookie(app.sid, "", secure=app.secure, max_age=0))
        return ctx.redirect("/", "Signed out.")

    @r.get("/account")
    def account(ctx):
        u = ctx.require()
        notes = db.all("SELECT * FROM notifications WHERE user_id=%s ORDER BY id DESC LIMIT 30", (u.id,))
        db.run("UPDATE notifications SET read=true WHERE user_id=%s", (u.id,))
        artists = db.all("SELECT a.*, l.name AS league_name FROM artists a JOIN leagues l ON l.slug=a.league_slug WHERE a.user_id=%s ORDER BY a.id", (u.id,))
        contracts = db.all("""SELECT c.*, l.name AS league_name FROM contracts c JOIN artists a ON a.id=c.artist_id JOIN leagues l ON l.slug=c.league_slug
            WHERE a.user_id=%s ORDER BY c.id DESC""", (u.id,))
        orders = db.all("SELECT * FROM orders WHERE buyer_user_id=%s AND status<>'pending' ORDER BY id DESC LIMIT 50", (u.id,))
        roles = sorted(u.roles) + [f"{r} in {s}" for s, rs in u.leagues.items() for r in sorted(rs)]
        body = M(f"<p class='eyebrow'>Account</p><h1>{h(u.name)}</h1><p>{h(u.email)} · {h(', '.join(roles))}</p>")
        body += M("<h2>Notifications</h2>") + (M("<ul>") + join(M(f"<li>{'' if n['read'] else '<strong>New:</strong> '}{h(n['message'])} <span class='muted'>{h(fmt_dt(n['at']))}</span></li>") for n in notes) + M("</ul>") if notes else M("<p class='muted'>No notifications.</p>"))
        body += M("<h2>Your emcee profiles</h2>") + table(["League", "Stage name", "Status", "Payout consent", ""], [[a["league_name"], a["stage_name"], a["status"], "Given " + fmt_dt(a["payout_consent_at"]) if a["payout_consent"] else "Not given",
                                                                                                                    M(f"<a href='/l/{h(a['league_slug'])}/artists/{a['id']}/edit'>Edit profile and consents</a>")] for a in artists], "No emcee profile yet.")
        body += M("<p><a href='/artist/signup'>Create an emcee profile</a></p>")
        if contracts:
            body += M("<h2>Contracts</h2>") + table(["League", "Title", "Terms", "Status", ""], [[c["league_name"], c["title"], c["terms"], c["status"],
                                                                                              (post_button(f"/account/contracts/{c['id']}", "Accept", ctx.csrf, fields={"decision": "accepted"}) + post_button(f"/account/contracts/{c['id']}", "Decline", ctx.csrf, fields={"decision": "declined"}, cls="secondary")) if c["status"] == "sent" else ""] for c in contracts])
        body += M("<h2>Purchases</h2>") + table(["When", "What", "Amount", "Status"], [[fmt_dt(o["paid_at"] or o["created_at"]), payments.KIND_LABEL[o["kind"]], payments.fmt_money(o["amount_cents"]), o["status"]] for o in orders], "No purchases.")
        body += (M("<section class='card'><h2>Change password</h2><form method='post' action='/account/password'>") + csrf_field(ctx.csrf) + field("Current password", "current", type_="password", required=True)
                 + field("New password", "password", type_="password", required=True, attrs="minlength='10'") + M("<button type='submit'>Change password</button></form></section>"))
        body += (M("<section class='card'><h2>Delete account</h2><p>Deletes your account, emcee profiles, photos and votes. Battle results keep the stage name as part of the public record.</p><form method='post' action='/account/delete'>")
                 + csrf_field(ctx.csrf) + field("Type DELETE to confirm", "confirm", required=True) + M("<button type='submit' class='danger'>Delete my account</button></form></section>"))
        return ctx.page("Account", body)

    @r.post("/account/password")
    def change_password(ctx):
        u = ctx.require()
        row = db.one("SELECT * FROM users WHERE id=%s", (u.id,))
        if row["bootstrap"]:
            raise HttpError(400, "The bootstrap admin password is the RAP_ADMIN_SECRET environment value")
        if not verify_password(row["password_hash"], str(ctx.form.get("current") or "")):
            raise HttpError(400, "Current password is incorrect")
        problem = password_problem(ctx.form.get("password"))
        if problem:
            raise HttpError(400, problem)
        db.run("UPDATE users SET password_hash=%s WHERE id=%s", (hash_password(ctx.form["password"]), u.id))
        app.sessions.destroy_all(u.id)
        start_session(ctx, u.id)
        audit(db, u.id, "password.changed")
        return ctx.redirect("/account", "Password changed; other sessions were signed out.")

    @r.post("/account/delete")
    def delete_account(ctx):
        u = ctx.require()
        if ctx.form.get("confirm") != "DELETE":
            raise HttpError(400, "Type DELETE to confirm")
        if u.bootstrap:
            raise HttpError(400, "The bootstrap admin cannot be deleted")
        with db.tx() as q:
            q.run("DELETE FROM uploads WHERE owner_user_id=%s", (u.id,))
            q.run("DELETE FROM users WHERE id=%s", (u.id,))
        audit(db, None, "account.deleted", {"userId": u.id})
        ctx.set_cookies.append(cookie(app.sid, "", secure=app.secure, max_age=0))
        return ctx.redirect("/", "Your account and data were deleted.")

    @r.post("/account/contracts/{cid}")
    def contract_decision(ctx, cid):
        u = ctx.require()
        decision = one_of(ctx.form.get("decision"), ("accepted", "declined"), "decision")
        c = db.one("SELECT c.* FROM contracts c JOIN artists a ON a.id=c.artist_id WHERE c.id=%s AND a.user_id=%s", (integer(cid, lo=1), u.id))
        if not c or c["status"] != "sent":
            raise HttpError(404, "Contract not found")
        db.run("UPDATE contracts SET status=%s, decided_at=now() WHERE id=%s", (decision, c["id"]))
        audit(db, u.id, "contract." + decision, {"contractId": c["id"]}, c["league_slug"])
        return ctx.redirect("/account", "Contract " + decision + ".")
