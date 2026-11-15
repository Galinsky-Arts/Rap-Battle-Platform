"""Operator configuration and entrypoints; never provisions upstream identity."""
import argparse
import os
from pathlib import Path
from wsgiref.simple_server import make_server, WSGIRequestHandler

from .app import Application
from .identity import NodeEngine, StubOperatorEngine
from .store import Store, PostgresStore


# Fictional fixtures required by the plan (NY / Atlanta / Los Angeles). They are
# not real promoters, venues or partnerships and are labelled synthetic everywhere.
SYNTHETIC_LEAGUES = (
    ("ny-synthetic", "synthetic-org-ny", "Harbor Lights Battle League (synthetic NY fixture)", "America/New_York",
     {"display_name": "Harbor Lights Battle League", "tagline": "Synthetic New York fixture league",
      "primary_color": "#ffcf5c", "accent_color": "#8ec5ff", "background_color": "#0d1424", "text_color": "#f4f6fb"}),
    ("atl-synthetic", "synthetic-org-atl", "Red Clay Cypher League (synthetic Atlanta fixture)", "America/New_York",
     {"display_name": "Red Clay Cypher League", "tagline": "Synthetic Atlanta fixture league",
      "primary_color": "#ff9e7a", "accent_color": "#b9f18c", "background_color": "#1c0f0c", "text_color": "#fbefe9"}),
    ("la-synthetic", "synthetic-org-la", "Sunset Grid Battle League (synthetic Los Angeles fixture)", "America/Los_Angeles",
     {"display_name": "Sunset Grid Battle League", "tagline": "Synthetic Los Angeles fixture league",
      "primary_color": "#7a2e00", "accent_color": "#004f7a", "background_color": "#fff6e8", "text_color": "#1f1300"}),
)


def seed_synthetic(store):
    """Idempotently create the three synthetic leagues with distinct branding."""
    for slug, org, name, tz, brand in SYNTHETIC_LEAGUES:
        if store.league(slug):
            continue
        try:
            store.add_league(slug, org, name, tz, synthetic=True)
            current = store.branding(slug)
            store.update_branding(slug, org, brand, None, current["version"])
        except Exception as error:
            # Another process seeded concurrently; the fixture already exists.
            if not any(t.__name__ in ("IntegrityError", "Conflict") for t in type(error).__mro__):
                raise


class TrustedProxyScheme:
    """Render terminates TLS and always sets X-Forwarded-Proto; only enable behind it.

    Only the scheme is taken from the proxy. Host is never taken from forwarded
    headers, so the app's exact Host/Origin checks remain in force.
    """

    def __init__(self, app):
        self.app = app

    def __call__(self, environ, start_response):
        if environ.get("HTTP_X_FORWARDED_PROTO", "").split(",")[0].strip() == "https":
            environ["wsgi.url_scheme"] = "https"
        return self.app(environ, start_response)


def create_store(env):
    if env.get("DATABASE_URL"):
        return PostgresStore(env["DATABASE_URL"])
    database = env.get("RAP_DATABASE")
    if not database:
        raise ValueError("DATABASE_URL or RAP_DATABASE is required")
    path = Path(database).resolve()
    if not path.parent.is_dir():
        raise ValueError("Create the private RAP_DATABASE directory before starting")
    return Store(path)


def create_engine(env):
    if env.get("RAP_ENGINE_CHECKOUT"):
        return NodeEngine(env["RAP_ENGINE_CHECKOUT"])
    if env.get("RAP_ADMIN_SECRET"):
        return StubOperatorEngine(env["RAP_ADMIN_SECRET"])
    return None


def resolve_origin(env):
    origin = env.get("RAP_ORIGIN") or env.get("RENDER_EXTERNAL_URL")
    if not origin:
        raise ValueError("RAP_ORIGIN (or Render's RENDER_EXTERNAL_URL) is required")
    return origin.rstrip("/")


def create_from_environment(environ=None):
    env = os.environ if environ is None else environ
    origin = resolve_origin(env)
    store = create_store(env)
    if env.get("RAP_SEED_SYNTHETIC") == "1":
        seed_synthetic(store)
    app = Application(store, origin, create_engine(env))
    return TrustedProxyScheme(app) if env.get("RAP_TRUST_PROXY_PROTO") == "1" else app


class PrivateRequestHandler(WSGIRequestHandler):
    def log_message(self, format, *args):
        # Default access logging includes full paths/query strings. The shell
        # intentionally emits no access bodies, credentials or tenant routes.
        pass


def main():
    parser = argparse.ArgumentParser(description="Local RAP shell liveness server; production uses gunicorn behind TLS")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()
    app = create_from_environment()
    # Plain HTTP is loopback-only; private routes still require genuine TLS scheme.
    with make_server("127.0.0.1", args.port, app, handler_class=PrivateRequestHandler) as server:
        server.serve_forever()


if __name__ == "__main__":
    main()
