"""Postgres/Neon backend parity. Runs only when TEST_DATABASE_URL is set (CI service)."""
import os
import unittest
from concurrent.futures import ThreadPoolExecutor

from rapbattle.presets import default_config
from rapbattle.runtime import SYNTHETIC_LEAGUES, seed_synthetic

URL = os.environ.get("TEST_DATABASE_URL")


@unittest.skipUnless(URL, "TEST_DATABASE_URL not set")
class PostgresStoreTests(unittest.TestCase):
    def setUp(self):
        import psycopg
        from rapbattle.store import PostgresStore
        with psycopg.connect(URL, autocommit=True) as db:
            db.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public")
        self.store = PostgresStore(URL)
        seed_synthetic(self.store)
        seed_synthetic(self.store)

    def tearDown(self):
        self.store.close()

    def test_seed_round_trip_and_isolation(self):
        for slug, org, name, tz, brand in SYNTHETIC_LEAGUES:
            league = self.store.league(slug)
            self.assertEqual((league["organization_id"], league["synthetic"]), (org, True))
            self.assertEqual(self.store.branding(slug)["primary_color"], brand["primary_color"])
        from rapbattle.store import Conflict
        with self.assertRaises(Conflict):
            self.store.update_settings("ny-synthetic", "synthetic-org-la", "Hijack", "UTC", 1)
        self.assertEqual(self.store.league("ny-synthetic")["organization_id"], "synthetic-org-ny")
        self.assertIsNone(self.store.league("' OR 1=1 --"))

    def test_concurrent_writes_have_one_winner(self):
        from rapbattle.store import Conflict

        def edit(name):
            try:
                self.store.update_settings("atl-synthetic", "synthetic-org-atl", name, "UTC", 1)
                return "updated"
            except Conflict:
                return "conflict"
        with ThreadPoolExecutor(max_workers=3) as pool:
            results = list(pool.map(edit, ["A", "B", "C"]))
        self.assertEqual(results.count("updated"), 1)
        self.assertEqual(self.store.league("atl-synthetic")["version"], 2)

    def test_presets_and_domain_mapping_persist(self):
        from rapbattle.store import Conflict, PostgresStore
        cfg = {**default_config("crew"), "side_size": 4}
        self.assertEqual(self.store.update_preset("la-synthetic", "synthetic-org-la", "crew", cfg, 0)["version"], 1)
        with self.assertRaises(Conflict):
            self.store.update_preset("la-synthetic", "synthetic-org-la", "crew", cfg, 0)
        b = self.store.branding("la-synthetic")
        brand = {k: b[k] for k in ("display_name", "tagline", "primary_color", "accent_color", "background_color", "text_color")}
        self.store.update_branding("la-synthetic", "synthetic-org-la", brand, "la.fixture.example", b["version"])
        n = self.store.branding("ny-synthetic")
        with self.assertRaises(Conflict):
            self.store.update_branding("ny-synthetic", "synthetic-org-ny",
                                       {k: n[k] for k in brand}, "la.fixture.example", n["version"])
        reopened = PostgresStore(URL)
        try:
            self.assertEqual(reopened.league_by_domain("la.fixture.example"), "la-synthetic")
            crew = next(p for p in reopened.presets("la-synthetic") if p["id"] == "crew")
            self.assertEqual(crew["config"]["side_size"], 4)
            self.assertTrue(reopened.healthy())
        finally:
            reopened.close()


if __name__ == "__main__":
    unittest.main()
