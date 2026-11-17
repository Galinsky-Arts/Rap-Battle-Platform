"""Minimal WSGI toolkit: requests, responses, routing, forms, multipart, cookies, escaping."""
import html as _html
import json
import re
from http.cookies import SimpleCookie, CookieError
from urllib.parse import parse_qs, quote

STATUS = {200: "OK", 204: "No Content", 303: "See Other", 400: "Bad Request", 401: "Unauthorized", 403: "Forbidden",
          404: "Not Found", 405: "Method Not Allowed", 409: "Conflict", 413: "Content Too Large", 415: "Unsupported Media Type",
          421: "Misdirected Request", 429: "Too Many Requests", 500: "Internal Server Error", 502: "Bad Gateway", 503: "Service Unavailable"}


class HttpError(Exception):
    def __init__(self, status, message, *, hard=False):
        super().__init__(message)
        self.status, self.message, self.hard = status, message, hard


class Markup(str):
    """Trusted HTML fragment. Concatenation escapes untrusted operands and stays Markup."""

    def __add__(self, other):
        return Markup(str.__add__(self, h(other)))

    def __radd__(self, other):
        return Markup(str.__add__(h(other), self))


def h(value):
    if isinstance(value, Markup):
        return value
    if value is None:
        return Markup("")
    return Markup(_html.escape(str(value), quote=True))


def join(parts):
    return Markup("".join(h(p) for p in parts))


class Response:
    def __init__(self, status=200, body=b"", content_type="text/html; charset=utf-8", headers=None):
        self.status, self.content_type = status, content_type
        self.body = body.encode("utf-8") if isinstance(body, str) else body
        self.headers = list(headers or [])


def redirect(location):
    return Response(303, "See " + location, "text/plain; charset=utf-8", [("Location", location)])


def json_response(status, obj):
    return Response(status, json.dumps(obj), "application/json; charset=utf-8")


def cookie(name, value, *, secure, max_age=None, http_only=True):
    parts = [f"{name}={quote(value, safe='')}", "Path=/", "SameSite=Lax"]
    if http_only:
        parts.append("HttpOnly")
    if secure:
        parts.append("Secure")
    if max_age is not None:
        parts.append(f"Max-Age={max_age}")
    return "; ".join(parts)


class Request:
    def __init__(self, environ):
        self.environ = environ
        self.method = environ.get("REQUEST_METHOD", "GET").upper()
        self.path = environ.get("PATH_INFO", "/") or "/"
        if len(self.path) > 1 and self.path.endswith("/"):
            self.path = self.path.rstrip("/")
        self.query = {k: v[0] for k, v in parse_qs(environ.get("QUERY_STRING", ""), keep_blank_values=True).items()}
        self.query_string = environ.get("QUERY_STRING", "")
        raw = environ.get("HTTP_COOKIE", "")
        self.cookies = {}
        try:
            jar = SimpleCookie()
            jar.load(raw)
            self.cookies = {k: m.value for k, m in jar.items()}
        except CookieError:
            pass
        self.form, self.files, self.params = {}, {}, {}

    def header(self, name):
        return self.environ.get("HTTP_" + name.upper().replace("-", "_"))

    def read_body(self, limit):
        try:
            length = int(self.environ.get("CONTENT_LENGTH") or 0)
        except ValueError:
            raise HttpError(400, "Bad length")
        if length > limit:
            raise HttpError(413, "Request too large")
        return self.environ["wsgi.input"].read(length) if length else b""

    def parse_form(self, *, multipart_limit=None):
        ctype = self.environ.get("CONTENT_TYPE", "")
        if ctype.startswith("application/x-www-form-urlencoded"):
            body = self.read_body(64 * 1024).decode("utf-8", "replace")
            self.form = {}
            for k, v in parse_qs(body, keep_blank_values=True).items():
                self.form[k] = v if len(v) > 1 else v[0]
        elif ctype.startswith("multipart/form-data") and multipart_limit:
            self.form, self.files = parse_multipart(self.read_body(multipart_limit), ctype)
        else:
            raise HttpError(415, "Unsupported form encoding")

    def get_list(self, key):
        v = self.form.get(key, [])
        return v if isinstance(v, list) else [v]


def parse_multipart(body, ctype):
    m = re.search(r'boundary=(?:"([^"]+)"|([^;]+))', ctype)
    if not m:
        raise HttpError(400, "Bad multipart body")
    boundary = b"--" + (m.group(1) or m.group(2)).strip().encode()
    fields, files = {}, {}
    parts = body.split(boundary)
    if len(parts) < 2 or len(parts) > 52:
        raise HttpError(400, "Bad multipart body")
    for part in parts[1:]:
        if part.startswith(b"--"):
            break
        part = part[2:] if part.startswith(b"\r\n") else part
        head, sep, data = part.partition(b"\r\n\r\n")
        if not sep:
            raise HttpError(400, "Bad multipart part")
        data = data[:-2] if data.endswith(b"\r\n") else data
        headers = head.decode("utf-8", "replace")
        name = re.search(r'\bname="([^"]*)"', headers)
        filename = re.search(r'\bfilename="([^"]*)"', headers)
        if not name:
            continue
        if filename:
            if data:
                files[name.group(1)] = {"filename": filename.group(1), "data": data}
        else:
            fields[name.group(1)] = data.decode("utf-8", "replace")
    return fields, files


class Router:
    def __init__(self):
        self.routes = []

    def add(self, method, pattern, handler, **opts):
        regex = re.compile("^" + re.sub(r"\{(\w+)\}", lambda m: f"(?P<{m.group(1)}>[A-Za-z0-9_-]{{1,128}})", pattern) + "$")
        self.routes.append((method, regex, handler, opts))

    def get(self, pattern, **opts):
        return lambda fn: (self.add("GET", pattern, fn, **opts), fn)[1]

    def post(self, pattern, **opts):
        return lambda fn: (self.add("POST", pattern, fn, **opts), fn)[1]

    def match(self, method, path):
        wrong_method = False
        for m, regex, handler, opts in self.routes:
            found = regex.match(path)
            if not found:
                continue
            if m != method and not (method == "HEAD" and m == "GET"):
                wrong_method = True
                continue
            return handler, found.groupdict(), opts
        if wrong_method:
            raise HttpError(405, "Method not allowed")
        return None


def text(value, *, max_len=200, min_len=0, name="Field"):
    s = str(value if value is not None else "").replace("\r\n", "\n").strip()
    if isinstance(value, list):
        raise HttpError(400, f"{name} is invalid")
    if len(s) < min_len:
        raise HttpError(400, f"{name} is required")
    if len(s) > max_len:
        raise HttpError(400, f"{name} is too long")
    if re.search(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", s):
        raise HttpError(400, f"{name} has invalid characters")
    return s


def integer(value, *, lo=None, hi=None, default=None, name="Number"):
    try:
        n = int(str(value).strip())
    except (TypeError, ValueError):
        if default is not None:
            return default
        raise HttpError(400, f"{name} must be a whole number")
    if (lo is not None and n < lo) or (hi is not None and n > hi):
        if default is not None:
            return default
        raise HttpError(400, f"{name} must be between {lo} and {hi}")
    return n


def money(value, *, name="Amount", allow_zero=True):
    s = str(value or "0").strip().lstrip("$")
    if not re.fullmatch(r"\d{1,6}(\.\d{1,2})?", s):
        raise HttpError(400, f"{name} must be an amount like 10 or 9.99")
    cents = int(round(float(s) * 100))
    if not allow_zero and cents < 50:
        raise HttpError(400, f"{name} must be at least 0.50")
    return cents


def checkbox(value):
    return value in ("on", "1", "true", "yes")


def one_of(value, options, name="value"):
    if value not in options:
        raise HttpError(400, f"Invalid {name}")
    return value
