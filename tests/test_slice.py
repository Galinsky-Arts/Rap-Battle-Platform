"""Deployable slice: RAP-02 branding, RAP-03 vocabulary in pages, RAP-04 presets API,
stub operator identity, host routing and cross-tenant negative tests."""
import json
import tempfile
import unittest
from pathlib import Path

from rapbattle.app import Application
from rapbattle.branding import contrast, validate_branding, validate_domain, stylesheet
from rapbattle.identity import StubOperatorEngine, IdentityError
from rapbattle.presets import default_config
from rapbattle.runtime import SYNTHETIC_LEAGUES, TrustedProxyScheme, create_from_environment, seed_synthetic
from rapbattle.store import Store, Conflict
from test_app import request, ORIGIN

SECRET = "s" * 48


class SliceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp.name) / "slice.sqlite")
        seed_synthetic(self.store)
        seed_synthetic(self.store)  # idempotent
        self.engine = StubOperatorEngine(SECRET)
        self.app = Application(self.store, ORIGIN, self.engine)
        self.tokens = {slug: self.engine.token_for(org) for slug, org, *_ in SYNTHETIC_LEAGUES}

    def tearDown(self):
        self.temp.cleanup()

    def call(self, path, method="GET", body=None, slug=None, token=None, extra=None):
        token = token if token is not None else (self.tokens[slug] if slug else None)
        return request(self.app, path, method, body, token=token, extra=extra)

    def test_three_synthetic_leagues_have_distinct_labelled_public_branding(self):
        pages = {}
        for slug, org, name, tz, brand in SYNTHETIC_LEAGUES:
            page = self.call(f"/l/{slug}", token=None)
            self.assertEqual(page["status"], 200)
            self.assertIn("Synthetic fixture", page["body"])
            self.assertIn(brand["display_name"], page["body"])
            self.assertNotIn(org, page["body"], "engine organization ids are private")
            css = self.call(f"/l/{slug}/brand.css", token=None)
            self.assertTrue(css["headers"]["Content-Type"].startswith("text/css"))
            self.assertIn(brand["primary_color"], css["body"])
            pages[slug] = page["body"]
        self.assertEqual(len({b for b in pages.values()}), 3)
        for slug, body in pages.items():
            for other, _, _, _, brand in SYNTHETIC_LEAGUES:
                if other != slug:
                    self.assertNotIn(brand["display_name"], body)
        self.assertEqual(self.call("/l/unknown-league", token=None)["status"], 404)
        self.assertEqual(self.call("/l/ny-synthetic", "POST", {}, token=None)["status"], 405)

    def test_public_formats_page_uses_battle_vocabulary_and_presets(self):
        page = self.call("/l/atl-synthetic/formats", token=None)
        self.assertEqual(page["status"], 200)
        for text in ("Battle formats", "Three-round battle", "Crew battle", "Tournament bracket", "Emcees per side",
                     "Judges", "Round timer"):
            self.assertIn(text, page["body"])

    def test_operator_token_is_scoped_to_exactly_one_league(self):
        ny, atl = self.tokens["ny-synthetic"], self.tokens["atl-synthetic"]
        self.assertEqual(self.call("/leagues/ny-synthetic", token=ny)["status"], 200)
        for slug in ("atl-synthetic", "la-synthetic"):
            self.assertEqual(self.call(f"/leagues/{slug}", token=ny)["status"], 403)
            self.assertEqual(self.call(f"/api/leagues/{slug}/branding", token=ny)["status"], 403)
            self.assertEqual(self.call(f"/api/leagues/{slug}/presets", token=ny)["status"], 403)
        self.assertEqual(self.call("/api/leagues/ny-synthetic/presets", token=atl)["status"], 403)
        self.assertEqual(self.call("/leagues/ny-synthetic", token="b" * 43)["status"], 403)
        self.assertEqual(self.call("/leagues/ny-synthetic", token="short")["status"], 401)
        with self.assertRaises(ValueError):
            StubOperatorEngine("too-short")
        self.assertFalse(self.engine.ready(), "stub never claims engine readiness")
        self.assertEqual(self.call("/readyz", token=None)["status"], 503)
        with self.assertRaises(IdentityError):
            self.engine.authorize("Bearer " + ny, "synthetic-org-ny", "payments.manage")

    def test_branding_round_trip_validation_and_cross_tenant_write_denied(self):
        current = json.loads(self.call("/api/leagues/la-synthetic/branding", slug="la-synthetic")["body"])
        brand = {k: current[k] for k in ("display_name", "tagline", "primary_color", "accent_color",
                                         "background_color", "text_color")}
        brand["tagline"] = "Updated synthetic tagline"
        ok = self.call("/api/leagues/la-synthetic/branding", "PUT",
                       {"branding": brand, "custom_domain": "battles.la-fixture.example", "version": current["version"]},
                       slug="la-synthetic")
        self.assertEqual(ok["status"], 200, ok["body"])
        self.assertEqual(json.loads(ok["body"])["version"], current["version"] + 1)
        stale = self.call("/api/leagues/la-synthetic/branding", "PUT",
                          {"branding": brand, "custom_domain": None, "version": current["version"]}, slug="la-synthetic")
        self.assertEqual(stale["status"], 409)
        hijack = self.call("/api/leagues/la-synthetic/branding", "PUT",
                           {"branding": brand, "custom_domain": None, "version": current["version"] + 1},
                           slug="ny-synthetic")
        self.assertEqual(hijack["status"], 403)
        ny = self.store.branding("ny-synthetic")
        steal = self.call("/api/leagues/ny-synthetic/branding", "PUT",
                          {"branding": {k: ny[k] for k in brand}, "custom_domain": "battles.la-fixture.example",
                           "version": ny["version"]}, slug="ny-synthetic")
        self.assertEqual(steal["status"], 409, "a domain cannot be mapped to two leagues")
        for bad in ({**brand, "text_color": "#fff6e9"}, {**brand, "primary_color": "red"},
                    {**brand, "display_name": "<b>\n</b>"}, {**brand, "extra": "x"}):
            r = self.call("/api/leagues/la-synthetic/branding", "PUT",
                          {"branding": bad, "custom_domain": None, "version": current["version"] + 1}, slug="la-synthetic")
            self.assertEqual(r["status"], 400)
        r = self.call("/api/leagues/la-synthetic/branding", "PUT",
                      {"branding": brand, "custom_domain": "evil.onrender.com", "version": current["version"] + 1},
                      slug="la-synthetic")
        self.assertEqual(r["status"], 400)

    def test_custom_domain_routes_only_to_its_league_public_pages(self):
        b = self.store.branding("ny-synthetic")
        self.store.update_branding("ny-synthetic", "synthetic-org-ny", {k: b[k] for k in (
            "display_name", "tagline", "primary_color", "accent_color", "background_color", "text_color")},
            "ny.battles.example", b["version"])
        host = {"HTTP_HOST": "ny.battles.example"}
        home = self.call("/", token=None, extra=host)
        self.assertEqual(home["status"], 200)
        self.assertIn("Harbor Lights Battle League", home["body"])
        self.assertIn("href='/brand.css'", home["body"])
        self.assertEqual(self.call("/brand.css", token=None, extra=host)["status"], 200)
        self.assertEqual(self.call("/formats", token=None, extra=host)["status"], 200)
        # Private and API routes are never served on a league's custom domain.
        for path in ("/leagues/ny-synthetic", "/api/leagues/ny-synthetic/presets", "/l/atl-synthetic"):
            self.assertEqual(self.call(path, slug="ny-synthetic", extra=host)["status"], 404)
        self.assertEqual(self.call("/", "PUT", {}, token=None, extra=host)["status"], 405)
        self.assertEqual(self.call("/", token=None, extra={**host, "wsgi.url_scheme": "http"})["status"], 421)
        self.assertEqual(self.call("/", token=None, extra={"HTTP_HOST": "unmapped.example"})["status"], 421)

    def test_presets_round_trip_and_validation(self):
        listing = json.loads(self.call("/api/leagues/ny-synthetic/presets", slug="ny-synthetic")["body"])
        self.assertEqual([p["version"] for p in listing["presets"]], [0] * 5)
        cfg = {**default_config("three-round"), "round_seconds": 75, "style": "freestyle"}
        saved = self.call("/api/leagues/ny-synthetic/presets/three-round", "PUT", {"config": cfg, "version": 0},
                          slug="ny-synthetic")
        self.assertEqual(saved["status"], 200, saved["body"])
        self.assertEqual(json.loads(saved["body"])["version"], 1)
        again = self.call("/api/leagues/ny-synthetic/presets/three-round", "PUT", {"config": cfg, "version": 0},
                          slug="ny-synthetic")
        self.assertEqual(again["status"], 409)
        bad = self.call("/api/leagues/ny-synthetic/presets/three-round", "PUT",
                        {"config": {**cfg, "rounds": 2}, "version": 1}, slug="ny-synthetic")
        self.assertEqual(bad["status"], 400)
        self.assertEqual(self.call("/api/leagues/ny-synthetic/presets/unknown", "PUT",
                                   {"config": cfg, "version": 0}, slug="ny-synthetic")["status"], 404)
        other = self.call("/api/leagues/atl-synthetic/presets/three-round", "PUT", {"config": cfg, "version": 0},
                          slug="ny-synthetic")
        self.assertEqual(other["status"], 403)
        atl = json.loads(self.call("/api/leagues/atl-synthetic/presets", slug="atl-synthetic")["body"])
        self.assertEqual(next(p for p in atl["presets"] if p["id"] == "three-round")["config"]["round_seconds"], 90)
        page = self.call("/leagues/ny-synthetic", slug="ny-synthetic")
        self.assertIn("3 rounds × 75s, freestyle", page["body"])
        self.assertIn("League settings", page["body"])
        with self.assertRaises(Conflict):
            self.store.update_preset("ny-synthetic", "synthetic-org-la", "three-round", cfg, 1)

    def test_branding_contrast_and_stylesheet_are_safe(self):
        for *_, brand in SYNTHETIC_LEAGUES:
            checked = validate_branding(brand)
            self.assertGreaterEqual(contrast(checked["text_color"], checked["background_color"]), 4.5)
            css = stylesheet(checked)
            self.assertNotIn("url(", css)
            self.assertNotIn("@import", css)
        for domain in ("https://x.example", "x.example:8080", "x.example/path", "localhost", "a..example"):
            with self.assertRaises(ValueError):
                validate_domain(domain)
        self.assertEqual(validate_domain(" Battles.Example.ORG "), "battles.example.org")

    def test_runtime_from_environment_with_stub_seed_and_proxy(self):
        env = {"RAP_ORIGIN": ORIGIN, "RAP_DATABASE": str(Path(self.temp.name) / "rt.sqlite"),
               "RAP_ADMIN_SECRET": SECRET, "RAP_SEED_SYNTHETIC": "1", "RAP_TRUST_PROXY_PROTO": "1"}
        app = create_from_environment(env)
        self.assertIsInstance(app, TrustedProxyScheme)
        r = request(app, "/l/la-synthetic", token=None,
                    extra={"wsgi.url_scheme": "http", "HTTP_X_FORWARDED_PROTO": "https"})
        self.assertEqual(r["status"], 200)
        spoof = request(app, "/l/la-synthetic", token=None,
                        extra={"wsgi.url_scheme": "http", "HTTP_X_FORWARDED_PROTO": "http"})
        self.assertEqual(spoof["status"], 421)
        health = request(app, "/healthz", token=None, extra={"HTTP_HOST": "10.0.0.1:10000", "wsgi.url_scheme": "http"})
        self.assertEqual(health["status"], 200)
        render = create_from_environment({"RENDER_EXTERNAL_URL": ORIGIN + "/", "RAP_DATABASE": env["RAP_DATABASE"]})
        self.assertEqual(render.origin, ORIGIN)
        with self.assertRaises(ValueError):
            create_from_environment({"RAP_DATABASE": env["RAP_DATABASE"]})


if __name__ == "__main__":
    unittest.main()
