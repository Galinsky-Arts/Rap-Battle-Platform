"""Full Rap Battle web application (WSGI). Composes accounts, leagues, artists, the built-in
battle engine, judging, voting, standings, sponsorship and payments; delegates the RAP-01..04
JSON/branding routes to the legacy Application so existing integrations keep working."""
import json
import re
import secrets
import sys
import traceback
from urllib.parse import quote, urlsplit

from .http import HttpError, Request, Response, Router, cookie, redirect, h, Markup, STATUS
from .security import Keyring, Sessions, RateLimiter, client_ip
from .settings import Settings
from .views import layout, APP_CSS, APP_JS, FRAME_SOURCES

CSP = (f"default-src 'none'; style-src 'self'; script-src 'self'; img-src 'self'; frame-src {FRAME_SOURCES}; "
       "form-action 'self' https://checkout.stripe.com; base-uri 'none'; frame-ancestors 'none'")
LEGACY = re.compile(r"^(/readyz|/assets/shell\.css|/api/.*|/l/[a-z0-9-]{1,63}/(brand\.css|formats)|/leagues/[a-z0-9-]{1,63})$")
TITLES = {400: "Check your input", 401: "Sign in required", 403: "Not allowed", 404: "Not found", 405: "Method not allowed", 409: "Not possible right now",
          413: "Too large", 415: "Unsupported", 429: "Slow down", 502: "Payment provider error", 503: "Unavailable"}


class Ctx:
    def __init__(self, app, req):
        self.app, self.req = app, req
        self.set_cookies, self.flash, self.user, self.session = [], None, None, None

    @property
    def form(self):
        return self.req.form

    def page(self, title, body, *, league=None, brand=None, status=200, notice=None, refresh=None):
        if league and brand is None:
            brand = self.app.brand(league)
        html_doc = layout(title=title, body=body, user=self.user, csrf=self.csrf, flash=self.flash, league=league, brand=brand, notice=notice, refresh=refresh)
        return Response(status, html_doc, headers=[("Content-Security-Policy", CSP)])

    def redirect(self, location, flash=None):
        if flash:
            data = flash if isinstance(flash, dict) else {"text": flash}
            self.set_cookies.append(cookie("rap_flash", json.dumps(data), secure=self.app.secure, max_age=60))
        return redirect(location)

    def require(self):
        if not self.user:
            raise HttpError(401, "Sign in required")
        return self.user

    def require_admin(self):
        if not self.require().is_admin:
            raise HttpError(403, "You do not have access to this page")

    def require_league(self, slug, *roles):
        self.require()
        if not self.user.has_league_role(slug, *roles):
            raise HttpError(403, "You do not have access to this league")


class WebApp:
    def __init__(self, *, db, store, engine, origin, legacy, secret=None, trust_proxy=False, stripe=None):
        from .. import payments
        self.db, self.store, self.engine, self.origin, self.legacy = db, store, engine, origin.rstrip("/"), legacy
        parsed = urlsplit(self.origin)
        self.host, self.scheme = parsed.netloc, parsed.scheme
        self.secure = parsed.scheme == "https"
        self.keys = Keyring(secret)
        self.sessions = Sessions(db)
        self.limiter = RateLimiter()
        self.settings = Settings(db)
        self.trust_proxy = trust_proxy
        self.stripe = stripe or payments.Stripe(None, None)
        self.sid = "__Host-rap_sid" if self.secure else "rap_sid"
        self.anon = "__Host-rap_anon" if self.secure else "rap_anon"
        self.router = Router()
        from . import routes_public, routes_account, routes_artists, routes_judge, routes_manage, routes_admin, routes_money
        for mod in (routes_public, routes_account, routes_artists, routes_judge, routes_manage, routes_admin, routes_money):
            mod.mount(self.router, self)

    def brand(self, slug):
        try:
            return self.store.branding(slug)
        except Exception:
            return None

    def league(self, slug):
        lg = self.store.league(slug) if re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", slug or "") else None
        if not lg:
            raise HttpError(404, "League not found")
        return lg

    def __call__(self, environ, start_response):
        path = environ.get("PATH_INFO") or "/"
        method = environ.get("REQUEST_METHOD", "GET")
        if path == "/healthz" and method in ("GET", "HEAD"):
            return self._health(start_response)
        if environ.get("HTTP_HOST") != self.host or LEGACY.match(path):
            return self.legacy(environ, start_response)
        if environ.get("wsgi.url_scheme") != self.scheme:
            return self._finish(start_response, Response(421, "HTTPS required", "text/plain; charset=utf-8"), [])
        if path == "/assets/app.css":
            return self._finish(start_response, Response(200, APP_CSS, "text/css; charset=utf-8", [("Cache-Control", "public, max-age=300")]), [])
        if path == "/assets/app.js":
            return self._finish(start_response, Response(200, APP_JS, "text/javascript; charset=utf-8", [("Cache-Control", "public, max-age=300")]), [])
        req = Request(environ)
        ctx = Ctx(self, req)
        ctx.ip = client_ip(environ, self.trust_proxy)
        ctx.ip_hash = self.keys.ip_hash(ctx.ip)
        try:
            s = self.sessions.load(req.cookies.get(self.sid))
        except Exception:
            return self._finish(start_response, Response(503, "Service unavailable", "text/plain; charset=utf-8"), [])
        ctx.session, ctx.user = s, (s or {}).get("user")
        anon = req.cookies.get(self.anon)
        if not s and not (anon and re.fullmatch(r"[\w-]{20,64}", anon)):
            anon = secrets.token_urlsafe(18)
            ctx.set_cookies.append(cookie(self.anon, anon, secure=self.secure, max_age=60 * 60 * 24 * 30))
        ctx.csrf = self.keys.csrf_for(s["csrf"] if s else anon)
        if req.cookies.get("rap_flash"):
            try:
                ctx.flash = json.loads(req.cookies["rap_flash"])
            except ValueError:
                pass
            ctx.set_cookies.append(cookie("rap_flash", "", secure=self.secure, max_age=0))
        try:
            found = self.router.match(req.method, req.path)
            if not found:
                raise HttpError(404, "Page not found")
            handler, params, opts = found
            req.params = params
            if req.method == "POST" and not opts.get("raw"):
                if not self.limiter.allow("post:" + ctx.ip, 240, 60):
                    raise HttpError(429, "Too many requests; wait a minute")
                origin = req.header("Origin")
                if origin and origin != self.origin:
                    raise HttpError(403, "Cross-site request rejected", hard=True)
                limit = (self.settings.all()["upload_max_kb"] * 2 + 256) * 1024 if opts.get("multipart") else None
                req.parse_form(multipart_limit=limit)
                if not isinstance(req.form.get("_csrf"), str) or not secrets.compare_digest(req.form["_csrf"], ctx.csrf):
                    raise HttpError(403, "Your form expired; reload the page and try again", hard=True)
            res = handler(ctx, **params)
        except HttpError as e:
            res = self._error(ctx, e)
        except Exception:
            print(json.dumps({"msg": "request failed", "path": req.path, "error": traceback.format_exc()[-2000:]}), file=sys.stderr)
            res = self._error(ctx, HttpError(500, "An unexpected error occurred. Please try again."))
        if req.method == "HEAD":
            res.body = b""
        return self._finish(start_response, res, ctx.set_cookies)

    def _error(self, ctx, e):
        req = ctx.req
        if e.status == 401 and req.method == "GET":
            return ctx.redirect("/login?next=" + quote(req.path + ("?" + req.query_string if req.query_string else ""), safe="/?=&"))
        back = self._back(req) if req.method == "POST" and e.status < 500 and not e.hard else None
        if back:
            return ctx.redirect(back, {"kind": "error", "text": e.message})
        title = TITLES.get(e.status, "Something went wrong")
        ctx.flash = None
        return ctx.page(title, Markup(f"<h1>{h(title)}</h1><p>{h(e.message)}</p><p><a href='/'>Return home</a></p>"), status=e.status if e.status in (400, 401, 403, 404, 405, 409, 413, 415, 429, 502, 503) else 500)

    def _back(self, req):
        ref = req.header("Referer")
        if not ref:
            return None
        u = urlsplit(ref)
        if f"{u.scheme}://{u.netloc}" != self.origin:
            return None
        return u.path + ("?" + u.query if u.query else "")

    def _health(self, start_response):
        body = {"status": "ok", "service": "rap-battle"}
        try:
            self.db.one("SELECT 1")
            s = self.settings.all()
            body.update(database="ok", engine=self.engine.kind, accounts="email-password", artistSignupOpen=bool(s["artist_signup_open"]), paymentsEnabled=self.stripe.enabled)
        except Exception:
            body.update(database="unavailable")
        return self._finish(start_response, Response(200, json.dumps(body), "application/json"), [])

    def _finish(self, start_response, res, cookies):
        headers = [("Content-Type", res.content_type), ("Content-Length", str(len(res.body))), ("Cache-Control", "no-store"),
                   ("X-Content-Type-Options", "nosniff"), ("Referrer-Policy", "same-origin"), ("X-Frame-Options", "DENY")]
        if self.secure:
            headers.append(("Strict-Transport-Security", "max-age=31536000"))
        if not any(k.lower() == "content-security-policy" for k, _ in res.headers):
            headers.append(("Content-Security-Policy", "default-src 'none'; frame-ancestors 'none'"))
        headers = [x for x in headers if not any(k.lower() == x[0].lower() for k, _ in res.headers)] + res.headers + [("Set-Cookie", c) for c in cookies]
        start_response(f"{res.status} {STATUS.get(res.status, 'OK')}", headers)
        return [res.body]
