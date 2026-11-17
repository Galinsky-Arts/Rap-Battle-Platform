"""In-process WSGI client for the full application (cookie jar, CSRF scraping, manual redirects)."""
import io
import json
import os
import re
import uuid
from http.cookies import SimpleCookie
from urllib.parse import urlencode, unquote

DB_URL = os.environ.get("TEST_DATABASE_URL")
ORIGIN = "https://rap.test"
ADMIN_SECRET = "bootstrap-secret-".ljust(48, "x")


def reset_database():
    import psycopg
    with psycopg.connect(DB_URL, autocommit=True) as c:
        c.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public")


def make_app(extra_env=None, stripe_opener=None):
    from rapbattle.store import PostgresStore
    from rapbattle.runtime import seed_synthetic, build_web_app
    from rapbattle.app import Application
    from rapbattle.identity import StubOperatorEngine
    reset_database()
    env = {"DATABASE_URL": DB_URL, "RAP_ADMIN_SECRET": ADMIN_SECRET, "RAP_TRUST_PROXY_PROTO": "1", **(extra_env or {})}
    store = PostgresStore(DB_URL)
    seed_synthetic(store)
    legacy = Application(store, ORIGIN, StubOperatorEngine(ADMIN_SECRET))
    app = build_web_app(env, store, legacy, ORIGIN, stripe_opener=stripe_opener)
    return app, store


class Client:
    def __init__(self, app, ip="10.0.0.1"):
        self.app, self.ip, self.jar, self.csrf = app, ip, {}, None

    def request(self, method, path, body=b"", ctype=None, headers=None):
        path, _, qs = path.partition("?")
        env = {"REQUEST_METHOD": method, "PATH_INFO": path, "QUERY_STRING": qs, "HTTP_HOST": "rap.test", "wsgi.url_scheme": "https",
               "wsgi.input": io.BytesIO(body), "CONTENT_LENGTH": str(len(body)), "REMOTE_ADDR": "127.0.0.1", "HTTP_X_FORWARDED_FOR": self.ip,
               "HTTP_COOKIE": "; ".join(f"{k}={v}" for k, v in self.jar.items())}
        if ctype:
            env["CONTENT_TYPE"] = ctype
        for k, v in (headers or {}).items():
            env["HTTP_" + k.upper().replace("-", "_")] = v
        out = {}

        def start(status, hdrs):
            out["status"], out["headers"] = int(status.split()[0]), hdrs
        data = b"".join(self.app(env, start))
        for k, v in out["headers"]:
            if k.lower() == "set-cookie":
                c = SimpleCookie()
                c.load(v)
                for name, morsel in c.items():
                    if morsel["max-age"] == "0":
                        self.jar.pop(name, None)
                    else:
                        self.jar[name] = morsel.coded_value
        text = data.decode("utf-8", "replace")
        m = re.search(r"name='_csrf' value='([^']+)'", text)
        if m:
            self.csrf = m.group(1)
        hd = {k.lower(): v for k, v in out["headers"]}
        flash = None
        if "rap_flash" in self.jar:
            try:
                flash = json.loads(unquote(self.jar["rap_flash"]))
            except ValueError:
                flash = None
        return {"status": out["status"], "body": text, "headers": hd, "location": hd.get("location"), "flash": flash, "raw": data}

    def get(self, path):
        r = self.request("GET", path)
        assert "Traceback" not in r["body"]
        return r

    def post(self, path, fields=None, *, files=None, csrf=True, headers=None):
        if not self.csrf:
            self.get("/login")
        self.jar.pop("rap_flash", None)
        fields = dict(fields or {})
        if csrf:
            fields.setdefault("_csrf", self.csrf)
        hdrs = {"Origin": ORIGIN, "Referer": ORIGIN + "/back", **(headers or {})}
        if files is not None:
            boundary = "----rap" + uuid.uuid4().hex
            parts = []
            for k, v in fields.items():
                parts.append(f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"\r\n\r\n{v}\r\n".encode())
            for k, (fname, data, mime) in files.items():
                parts.append(f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"; filename=\"{fname}\"\r\nContent-Type: {mime}\r\n\r\n".encode() + data + b"\r\n")
            body = b"".join(parts) + f"--{boundary}--\r\n".encode()
            return self.request("POST", path, body, "multipart/form-data; boundary=" + boundary, hdrs)
        items = []
        for k, v in fields.items():
            for x in (v if isinstance(v, list) else [v]):
                items.append((k, x))
        return self.request("POST", path, urlencode(items).encode(), "application/x-www-form-urlencoded", hdrs)

    def login(self, email, password):
        self.get("/login")
        r = self.post("/login", {"email": email, "password": password})
        self.get("/account")
        return r

    def register(self, name, email, password="correct horse battery"):
        self.get("/register")
        r = self.post("/register", {"name": name, "email": email, "password": password})
        self.get("/account")
        return r


PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
