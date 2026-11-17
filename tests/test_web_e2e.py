"""RAP-12 production validation on Postgres through the real WSGI surface: three branded leagues,
accounts, gated artist sign-up, one-round and three-round battles, judges and crowd votes, ties with
overtime, brackets, standings, disputes, sponsorship, payment-off and payment-on (mocked Stripe API),
revenue splits, refunds, settlements and tenant isolation."""
import hashlib
import hmac
import io
import json
import re
import time
import unittest

from web_helpers import DB_URL, ADMIN_SECRET, make_app, Client, PNG

PRIVACY = {"operator_name": "Synthetic Operator", "privacy_contact": "privacy@example.test", "retention_days": "90", "policy_version": "v1",
           "policy_text": "Synthetic test policy.", "vote_min_account_minutes": "0", "vote_ip_cap": "25", "upload_max_kb": "64"}


def ids(body, pattern):
    return [int(x) for x in re.findall(pattern, body)]


class FakeStripe:
    """Records Checkout/refund calls and returns Stripe-shaped JSON (test double only)."""

    def __init__(self):
        self.calls = []

    def __call__(self, req, timeout=None):
        body = dict(x.split("=", 1) for x in req.data.decode().split("&"))
        self.calls.append((req.full_url, body))
        if req.full_url.endswith("checkout/sessions"):
            out = {"id": f"cs_test_{len(self.calls)}", "url": f"https://checkout.stripe.com/c/pay/cs_test_{len(self.calls)}"}
        else:
            out = {"id": "re_test", "status": "succeeded"}
        return io.BytesIO(json.dumps(out).encode())


def signed(secret, event):
    payload = json.dumps(event).encode()
    t = int(time.time())
    sig = hmac.new(secret.encode(), f"{t}.".encode() + payload, hashlib.sha256).hexdigest()
    return payload, f"t={t},v1={sig}"


@unittest.skipUnless(DB_URL, "TEST_DATABASE_URL not set")
class FullPlatformTests(unittest.TestCase):
    def setUp(self):
        self.stripe = FakeStripe()
        env = {"STRIPE_SECRET_KEY": "sk_test_fake", "STRIPE_WEBHOOK_SECRET": "whsec_fake"} if self._testMethodName.endswith("payments_on") else {}
        self.app, self.store = make_app(env, stripe_opener=self.stripe)
        self.addCleanup(self.app.db.close)
        self.addCleanup(self.store.close)
        self.admin = Client(self.app, "10.0.0.1")
        self.assertEqual(self.admin.login("admin", ADMIN_SECRET)["location"], "/account")

    def open_signup(self):
        self.admin.get("/admin/settings")
        r = self.admin.post("/admin/settings/signup", {"open": "1"})
        self.assertIn("policy version", r["flash"]["text"])
        self.admin.post("/admin/settings", PRIVACY)
        r = self.admin.post("/admin/settings/signup", {"open": "1"})
        self.assertEqual(r["flash"]["text"], "Emcee sign-up is open.")

    def user(self, name, ip):
        c = Client(self.app, ip)
        c.register(name, name.lower().replace(" ", ".") + "@example.test")
        return c

    def emcee(self, name, ip, league="ny-synthetic", payout=False):
        c = self.user(name, ip)
        c.get("/artist/signup?league=" + league)
        r = c.post("/artist/signup", {"league": league, "stage_name": name, "hometown": "Synthetic City", "style": "both", "bio": "Fictional emcee.",
                                      "link1": "https://youtu.be/abcdefghijk", "visibility": "public", "consent_profile": "on", "consent_media": "on"},
                   files={"photo": ("p.png", PNG, "image/png")})
        self.assertEqual(r["flash"]["text"], "Profile created.", r)
        aid = int(r["location"].rsplit("/", 1)[1])
        if payout:
            c.get(f"/l/{league}/artists/{aid}/edit")
            c.post(f"/l/{league}/artists/{aid}/edit", {"stage_name": name, "hometown": "Synthetic City", "style": "both", "bio": "Fictional emcee.", "link1": "https://youtu.be/abcdefghijk",
                                                       "visibility": "public", "consent_media": "on", "payout_consent": "on"}, files={})
        c.artist_id = aid
        return c

    def staff(self, league="ny-synthetic"):
        promoter = self.user("Promo Pat", "10.0.1.1")
        judges = [self.user(f"Judge {i}", f"10.0.2.{i}") for i in range(1, 4)]
        self.admin.get("/admin/users")
        self.admin.post("/admin/memberships", {"email": "promo.pat@example.test", "league": league, "role": "producer"})
        for i in range(1, 4):
            promoter.get(f"/manage/{league}")
            promoter.post(f"/manage/{league}/staff", {"email": f"judge.{i}@example.test", "role": "judge"})
        promoter.get("/account")
        return promoter, judges

    def run_battle(self, promoter, judges, league, mid, rounds, scores_for, fans=(), votes=()):
        base = f"/manage/{league}/battles/{mid}"
        promoter.get(base)
        self.assertEqual(promoter.post(base + "/start")["flash"]["text"], "Battle started.")
        if fans:
            promoter.post(base + "/voting", {"open": "1"})
            for fan, side in zip(fans, votes):
                fan.get(f"/l/{league}/battles/{mid}")
                self.assertEqual(fan.post(f"/l/{league}/battles/{mid}/vote", {"side": side})["flash"]["text"], "Vote recorded.")
        for _ in range(rounds * 2):
            self.assertEqual(promoter.post(base + "/timer", {"action": "start"})["flash"]["text"], "Timer updated.")
            promoter.post(base + "/timer", {"action": "stop"})
        crit = [c["id"] for c in self.app.engine.criteria(self.app.engine.match(mid)["event_id"])]
        m = self.app.engine.match(mid)
        first = m["current_round"] - rounds + 1
        for j, judge in enumerate(judges):
            judge.get(f"/judge/battles/{mid}")
            fields = {}
            for rnd in range(first, m["current_round"] + 1):
                a, b = scores_for(j, rnd)
                for c in crit:
                    fields[f"s_{rnd}_a_{c}"], fields[f"s_{rnd}_b_{c}"] = a, b
            self.assertEqual(judge.post(f"/judge/battles/{mid}", fields)["flash"]["text"], "Scores saved.")
        return promoter.post(base + "/close")["flash"]["text"]

    def test_three_leagues_full_live_operations_payments_off(self):
        a = self.app
        self.assertIn("not open", Client(a, "10.9.9.9").get("/artist/signup")["body"])
        self.open_signup()
        promoter, judges = self.staff()
        e1, e2, e3, e4 = (self.emcee(n, f"10.0.3.{i}") for i, n in enumerate(["Ace Synth", "Bolt Synth", "Cipher Synth", "Dune Synth"], 1))
        fans = [self.user(f"Fan {i}", f"10.0.4.{i}") for i in range(1, 4)]
        # Duplicate profile in the same league is refused; bad upload refused.
        e1.get("/artist/signup")
        self.assertIn("already have a profile", e1.post("/artist/signup", {"league": "ny-synthetic", "stage_name": "X", "style": "both", "visibility": "public", "consent_profile": "on"}, files={})["flash"]["text"])
        r = e1.post("/artist/signup", {"league": "atl-synthetic", "stage_name": "X", "style": "both", "visibility": "public", "consent_profile": "on"}, files={"photo": ("x.png", b"<svg/>", "image/png")})
        self.assertIn("JPEG, PNG or WebP", r["flash"]["text"])
        # Public profile with photo, private profile hidden
        self.assertIn("/uploads/", fans[0].get(f"/l/ny-synthetic/artists/{e1.artist_id}")["body"])

        # League settings and sponsorship opt-in
        promoter.get("/manage/ny-synthetic")
        promoter.post("/manage/ny-synthetic/settings", {"city": "New York", "region": "NY", "description": "Synthetic fixture league.", "listed": "on", "sponsorship_enabled": "on"})
        self.assertIn("Harbor Lights", fans[0].get("/leagues?region=NY")["body"])
        self.assertNotIn("Red Clay", fans[0].get("/leagues?region=NY")["body"])
        promoter.post("/manage/ny-synthetic/seasons", {"name": "Season 1", "win": "3", "draw": "1", "loss": "0"})
        season = a.db.one("SELECT id FROM seasons WHERE league_slug='ny-synthetic'")["id"]

        # ---- Event A: one-round battle with crowd vote counted as a ballot ----
        promoter.get("/manage/ny-synthetic/events")
        r = promoter.post("/manage/ny-synthetic/events", {"title": "Synthetic One-Round Night", "event_slug": "one-round-night", "preset": "one-round", "season_id": str(season), "starts_at": "2026-12-01T20:00", "venue": "Fictional Hall"})
        ev_a = int(r["location"].rsplit("/", 1)[1])
        a.db.run("UPDATE events SET config = jsonb_set(config, '{audience_vote_enabled}', 'true') WHERE id=%s", (ev_a,))
        promoter.get(f"/manage/ny-synthetic/events/{ev_a}")
        promoter.post(f"/manage/ny-synthetic/events/{ev_a}", {"title": "Synthetic One-Round Night", "starts_at": "2026-12-01T20:00", "venue": "Fictional Hall", "description": "", "status": "scheduled",
                                                              "published": "on", "season_id": str(season), "stream_url": "https://www.youtube.com/watch?v=abcdefghijk", "voting_enabled": "on", "ticket": "10", "ppv": "0"})
        # Lineup via artist requests
        for e in (e1, e2):
            e.get(f"/l/ny-synthetic/artists/{e.artist_id}/edit")
            self.assertEqual(e.post(f"/l/ny-synthetic/artists/{e.artist_id}/apply", {"event_id": str(ev_a), "note": "Ready"})["flash"]["text"], "Lineup request sent.")
        page = promoter.get(f"/manage/ny-synthetic/events/{ev_a}")["body"]
        for rid in ids(page, r"action='/manage/ny-synthetic/lineup/(\d+)'")[::2]:
            promoter.post(f"/manage/ny-synthetic/lineup/{rid}", {"decision": "accepted"})
        for i in range(1, 4):
            uid = a.db.one("SELECT id FROM users WHERE email=%s", (f"judge.{i}@example.test",))["id"]
            promoter.post(f"/manage/ny-synthetic/events/{ev_a}/judges", {"user_id": str(uid)})
        ents = a.engine.entrants(ev_a)
        self.assertEqual([x["display_name"] for x in ents], ["Ace Synth", "Bolt Synth"])
        promoter.post(f"/manage/ny-synthetic/events/{ev_a}/matches", {"side_a": str(ents[0]["id"]), "side_b": str(ents[1]["id"])})
        mid = a.engine.matches(ev_a)[0]["id"]
        # Ticket shown but payments are off
        ev_page = fans[0].get("/l/ny-synthetic/events/one-round-night")["body"]
        self.assertIn("online payments are switched off", ev_page)
        self.assertIn("youtube-nocookie.com/embed/abcdefghijk", ev_page)
        self.assertIn("switched off", fans[0].post("/pay/checkout", {"kind": "ticket", "event_id": str(ev_a)})["flash"]["text"])
        # Judges split 2-1 for A; crowd 1-2 favours B → ballots 2-2 → overtime (one-round preset allows 1 overtime)
        out = self.run_battle(promoter, judges, "ny-synthetic", mid, 1, lambda j, rnd: ("9", "7") if j < 2 else ("6", "8"), fans=fans, votes=["a", "b", "b"])
        self.assertEqual(out, "Tie: overtime round added.")
        # Anti-abuse: double vote, emcee self vote, judge vote, unauthenticated vote
        self.assertIn("already voted", fans[0].post(f"/l/ny-synthetic/battles/{mid}/vote", {"side": "b"})["flash"]["text"])
        self.assertIn("own battle", e1.post(f"/l/ny-synthetic/battles/{mid}/vote", {"side": "a"})["flash"]["text"])
        self.assertIn("Judges cannot", judges[0].post(f"/l/ny-synthetic/battles/{mid}/vote", {"side": "a"})["flash"]["text"])
        self.assertTrue(Client(a, "10.8.8.8").post(f"/l/ny-synthetic/battles/{mid}/vote", {"side": "a"})["location"].startswith("/login"))
        # Overtime round: judges unanimous for A (crowd stays B) → A wins 3-1
        base = f"/manage/ny-synthetic/battles/{mid}"
        for _ in range(2):
            promoter.post(base + "/timer", {"action": "start"})
            promoter.post(base + "/timer", {"action": "pause"})
            promoter.post(base + "/timer", {"action": "resume"})
            promoter.post(base + "/timer", {"action": "stop"})
        crit = [c["id"] for c in a.engine.criteria(ev_a)]
        for judge in judges:
            judge.get(f"/judge/battles/{mid}")
            judge.post(f"/judge/battles/{mid}", {**{f"s_2_a_{c}": "9" for c in crit}, **{f"s_2_b_{c}": "5" for c in crit}})
        self.assertIn("between 0 and 10", judges[0].post(f"/judge/battles/{mid}", {f"s_2_a_{crit[0]}": "11"})["flash"]["text"])
        self.assertEqual(promoter.post(base + "/close")["flash"]["text"], "Decision recorded.")
        m = a.engine.match(mid)
        self.assertEqual((m["status"], m["winner"], m["overtimes"]), ("decided", ents[0]["id"], 1))
        self.assertEqual(len(a.engine.turns(mid)), 4)
        self.assertEqual(a.engine.vote_counts(mid), {"a": 1, "b": 2})
        promoter.post(f"/manage/ny-synthetic/events/{ev_a}/finalize")
        self.assertEqual(a.engine.event(ev_a)["champion_entrant_id"], ents[0]["id"])

        # ---- Event B: three-round seeded bracket of four with a forfeit and a draw policy ----
        r = promoter.post("/manage/ny-synthetic/events", {"title": "Synthetic Three-Round Bracket", "event_slug": "three-round-bracket", "preset": "three-round", "season_id": str(season)})
        ev_b = int(r["location"].rsplit("/", 1)[1])
        promoter.post(f"/manage/ny-synthetic/events/{ev_b}", {"title": "Synthetic Three-Round Bracket", "status": "scheduled", "published": "on", "season_id": str(season), "voting_enabled": "", "ticket": "0", "ppv": "0"})
        for e in (e1, e2, e3, e4):
            promoter.post(f"/manage/ny-synthetic/events/{ev_b}/entrants", {"artist_id": str(e.artist_id)})
        for i in range(1, 4):
            uid = a.db.one("SELECT id FROM users WHERE email=%s", (f"judge.{i}@example.test",))["id"]
            promoter.post(f"/manage/ny-synthetic/events/{ev_b}/judges", {"user_id": str(uid)})
        promoter.post(f"/manage/ny-synthetic/events/{ev_b}/seed")
        seeds = {x["display_name"]: x["seed"] for x in a.engine.entrants(ev_b)}
        self.assertEqual(seeds["Ace Synth"], 1)  # standings leader seeds first
        self.assertIn("2 stage", promoter.post(f"/manage/ny-synthetic/events/{ev_b}/bracket")["flash"]["text"])
        ms = a.engine.matches(ev_b)
        semis, final = [m for m in ms if m["stage"] == 1], [m for m in ms if m["stage"] == 2][0]
        self.assertEqual(len(semis), 2)
        self.assertIn("Every battle must be finished", promoter.post(f"/manage/ny-synthetic/events/{ev_b}/finalize")["flash"]["text"])
        # Semi 1: side A wins all three rounds
        self.assertEqual(self.run_battle(promoter, judges, "ny-synthetic", semis[0]["id"], 3, lambda j, rnd: ("8", "6")), "Decision recorded.")
        # Semi 2: side B no-shows
        promoter.post(f"/manage/ny-synthetic/battles/{semis[1]['id']}/forfeit", {"side": "b", "reason": "no-show"})
        final = a.engine.match(final["id"])
        self.assertTrue(final["side_a"] and final["side_b"])
        self.assertEqual(self.run_battle(promoter, judges, "ny-synthetic", final["id"], 3, lambda j, rnd: ("5", "9")), "Decision recorded.")
        promoter.post(f"/manage/ny-synthetic/events/{ev_b}/finalize")
        champ = a.engine.event(ev_b)["champion_entrant_id"]
        self.assertEqual(champ, a.engine.match(final["id"])["winner"])
        pub = fans[0].get("/l/ny-synthetic/events/three-round-bracket")["body"]
        self.assertIn("Champion:", pub)
        self.assertIn("Bracket", pub)

        # Standings / records with style filter, dispute + correction
        st = fans[0].get(f"/l/ny-synthetic/standings?season={season}")["body"]
        self.assertIn("Ace Synth", st)
        rows = a.engine.standings("ny-synthetic", season)
        self.assertEqual(sum(r["played"] for r in rows), 2 * 4)
        fans[1].get(f"/l/ny-synthetic/battles/{mid}")
        self.assertIn("Dispute submitted", fans[1].post(f"/l/ny-synthetic/battles/{mid}/dispute", {"reason": "The overtime timer ran long for side A."})["flash"]["text"])
        promoter.get("/manage/ny-synthetic/disputes")
        did = a.db.one("SELECT id FROM disputes")["id"]
        promoter.post(f"/manage/ny-synthetic/battles/{mid}/correct", {"winner": "draw"})
        promoter.post(f"/manage/ny-synthetic/disputes/{did}", {"status": "upheld", "resolution": "Corrected to a draw."})
        self.assertEqual(a.engine.match(mid)["status"], "draw")
        self.assertIn("upheld", fans[1].get("/account")["body"])

        # Sponsorship: placement approved shows with impressions; offline invoice splits 15/85
        promoter.get("/manage/ny-synthetic/sponsors")
        promoter.post("/manage/ny-synthetic/sponsors", {"name": "Synthetic Sponsor Co", "url": "https://sponsor.example", "contact_email": "s@example.test"})
        sid = a.db.one("SELECT id FROM sponsors")["id"]
        promoter.post("/manage/ny-synthetic/placements", {"slot": "league_banner", "event_id": "", "sponsor_id": str(sid), "message": "Fictional drinks", "price": "100"})
        pid = a.db.one("SELECT id FROM placements")["id"]
        self.assertNotIn("Synthetic Sponsor Co", fans[0].get("/l/ny-synthetic")["body"])
        promoter.post(f"/manage/ny-synthetic/placements/{pid}", {"status": "approved"})
        self.assertIn("Synthetic Sponsor Co", fans[0].get("/l/ny-synthetic")["body"])
        self.assertEqual(a.db.one("SELECT impressions FROM placements WHERE id=%s", (pid,))["impressions"], 1)
        promoter.post("/manage/ny-synthetic/invoices", {"sponsor_id": str(sid), "placement_id": str(pid), "amount": "100"})
        inv = a.db.one("SELECT id FROM invoices")["id"]
        promoter.post(f"/manage/ny-synthetic/invoices/{inv}/offline", {"note": "Bank transfer REF-1"})
        parts = {r_["party"]: r_["s"] for r_ in a.db.all("SELECT party, sum(amount_cents)::int s FROM ledger GROUP BY party")}
        self.assertEqual(parts, {"platform": 1500, "league": 8500})
        self.assertIn("Settlement #", promoter.post("/manage/ny-synthetic/settle")["flash"]["text"])

        # Tenant isolation and cross-role negatives
        self.assertEqual(promoter.get("/manage/atl-synthetic")["status"], 403)
        self.assertEqual(promoter.post("/manage/atl-synthetic/settings", {"city": "x"})["flash"]["text"], "You do not have access to this league")
        self.assertEqual(fans[0].get("/manage/ny-synthetic")["status"], 403)
        self.assertEqual(fans[0].get("/admin")["status"], 403)
        self.assertEqual(judges[0].get(f"/manage/ny-synthetic/battles/{mid}")["status"], 403)
        self.assertEqual(a.engine.list_events("atl-synthetic"), [])
        self.assertEqual(fans[0].post("/manage/ny-synthetic/settle", csrf=False)["status"], 403)
        self.assertEqual(fans[0].post("/register", {"name": "x"}, headers={"Origin": "https://evil.example"})["status"], 403)
        # Three independently branded leagues render distinct names
        for slug, name in (("ny-synthetic", "Harbor Lights"), ("atl-synthetic", "Red Clay"), ("la-synthetic", "Sunset Grid")):
            self.assertIn(name, fans[0].get(f"/l/{slug}")["body"])
        # Render sweep: no escaped markup or template leftovers on any page
        pages = ["/", "/leagues", "/privacy", "/account", "/judge", "/manage", "/admin", "/admin/settings", "/admin/users", "/admin/audit", "/l/ny-synthetic", "/l/ny-synthetic/events",
                 "/l/ny-synthetic/artists", f"/l/ny-synthetic/artists/{e1.artist_id}", f"/l/ny-synthetic/artists/{e1.artist_id}/edit", "/l/ny-synthetic/standings",
                 "/l/ny-synthetic/events/one-round-night", "/l/ny-synthetic/events/three-round-bracket", f"/l/ny-synthetic/battles/{mid}", f"/judge/battles/{final['id']}"]
        manage = ["/manage/ny-synthetic", "/manage/ny-synthetic/events", f"/manage/ny-synthetic/events/{ev_a}", f"/manage/ny-synthetic/events/{ev_b}", f"/manage/ny-synthetic/battles/{mid}",
                  "/manage/ny-synthetic/artists", "/manage/ny-synthetic/disputes", "/manage/ny-synthetic/sponsors", "/manage/ny-synthetic/revenue", f"/sponsor/invoices/{inv}"]
        for who, paths in ((self.admin, pages + manage), (promoter, manage), (judges[0], ["/judge", f"/judge/battles/{final['id']}"]), (e1, pages[:5] + [f"/l/ny-synthetic/artists/{e1.artist_id}/edit"])):
            for p in paths:
                r = who.get(p)
                self.assertIn(r["status"], (200, 403, 404), p)
                self.assertNotRegex(r["body"], r"&lt;(form|a |p|div|section|input|span|strong|li|h[1-6]|label|code|br|td)|\[object|None</|>None<", p)
        # Privacy: changing the policy closes sign-up; deleting the account removes the profile but keeps results
        r = self.admin.post("/admin/settings", {**PRIVACY, "policy_version": "v2"})
        self.assertIn("closed", r["flash"]["text"])
        e4.get("/account")
        e4.post("/account/delete", {"confirm": "DELETE"})
        self.assertIsNone(a.db.one("SELECT 1 FROM artists WHERE id=%s", (e4.artist_id,)))
        self.assertEqual(a.db.one("SELECT count(*) n FROM entrants WHERE display_name='Dune Synth'")["n"], 1)

    def test_creator_economy_and_revenue_with_payments_on(self):
        a = self.app
        self.assertTrue(a.stripe.enabled)
        self.open_signup()
        promoter, judges = self.staff("la-synthetic")
        emc = self.emcee("Echo Synth", "10.1.3.1", "la-synthetic", payout=True)
        other = self.emcee("Flux Synth", "10.1.3.2", "la-synthetic", payout=False)
        fan = self.user("Fan Payer", "10.1.4.1")
        promoter.get("/manage/la-synthetic/events")
        r = promoter.post("/manage/la-synthetic/events", {"title": "Synthetic PPV Night", "event_slug": "ppv-night", "preset": "one-v-one"})
        ev = int(r["location"].rsplit("/", 1)[1])
        promoter.post(f"/manage/la-synthetic/events/{ev}", {"title": "Synthetic PPV Night", "status": "scheduled", "published": "on", "ticket": "20", "ppv": "8", "stream_url": "https://vimeo.com/123456789"})
        for e in (emc, other):
            promoter.post(f"/manage/la-synthetic/events/{ev}/entrants", {"artist_id": str(e.artist_id)})
        page = fan.get("/l/la-synthetic/events/ppv-night")["body"]
        self.assertNotIn("player.vimeo.com", page)  # PPV stream locked
        # Ticket checkout → Stripe session → signed webhook marks paid and splits 10/60/30 (event pool)
        r = fan.post("/pay/checkout", {"kind": "ticket", "event_id": str(ev)})
        self.assertTrue(r["location"].startswith("https://checkout.stripe.com/"))
        order = a.db.one("SELECT * FROM orders ORDER BY id DESC LIMIT 1")
        self.assertEqual((order["kind"], order["amount_cents"], order["status"]), ("ticket", 2000, "pending"))
        url, sent = self.stripe.calls[-1]
        self.assertEqual(sent["line_items%5B0%5D%5Bprice_data%5D%5Bunit_amount%5D"], "2000")
        payload, sig = signed("whsec_fake", {"id": "evt_1", "type": "checkout.session.completed", "data": {"object": {"id": order["stripe_session_id"], "payment_status": "paid", "amount_total": 2000, "payment_intent": "pi_1", "metadata": {"order_id": str(order["id"])}}}})
        bad = Client(a, "10.1.9.9").request("POST", "/stripe/webhook", payload, "application/json", {"Stripe-Signature": sig.replace("v1=", "v1=0")})
        self.assertEqual(bad["status"], 400)
        ok = Client(a, "10.1.9.9").request("POST", "/stripe/webhook", payload, "application/json", {"Stripe-Signature": sig})
        self.assertEqual(json.loads(ok["body"])["result"], "paid")
        dup = Client(a, "10.1.9.9").request("POST", "/stripe/webhook", payload, "application/json", {"Stripe-Signature": sig})
        self.assertEqual(json.loads(dup["body"])["result"], "duplicate")
        parts = {r_["party"]: r_["s"] for r_ in a.db.all("SELECT party, sum(amount_cents)::int s FROM ledger GROUP BY party")}
        self.assertEqual(parts, {"platform": 200, "league": 1200, "event_pool": 600})
        # PPV purchase unlocks the stream
        fan.post("/pay/checkout", {"kind": "ppv", "event_id": str(ev)})
        o2 = a.db.one("SELECT * FROM orders ORDER BY id DESC LIMIT 1")
        payload, sig = signed("whsec_fake", {"id": "evt_2", "type": "checkout.session.completed", "data": {"object": {"id": o2["stripe_session_id"], "payment_status": "paid", "amount_total": 800, "payment_intent": "pi_2", "metadata": {"order_id": str(o2["id"])}}}})
        Client(a).request("POST", "/stripe/webhook", payload, "application/json", {"Stripe-Signature": sig})
        self.assertIn("player.vimeo.com/video/123456789", fan.get("/l/la-synthetic/events/ppv-night")["body"])
        # Tip only for emcees with payout consent; artist share 90%
        self.assertIn("Support this emcee", fan.get(f"/l/la-synthetic/artists/{emc.artist_id}")["body"])
        self.assertNotIn("Support this emcee", fan.get(f"/l/la-synthetic/artists/{other.artist_id}")["body"])
        self.assertIn("not accepting support", fan.post("/pay/checkout", {"kind": "tip", "artist_id": str(other.artist_id), "amount": "5"})["flash"]["text"])
        fan.post("/pay/checkout", {"kind": "tip", "artist_id": str(emc.artist_id), "amount": "5"})
        o3 = a.db.one("SELECT * FROM orders ORDER BY id DESC LIMIT 1")
        payload, sig = signed("whsec_fake", {"id": "evt_3", "type": "checkout.session.completed", "data": {"object": {"id": o3["stripe_session_id"], "payment_status": "paid", "amount_total": 500, "payment_intent": "pi_3", "metadata": {"order_id": str(o3["id"])}}}})
        Client(a).request("POST", "/stripe/webhook", payload, "application/json", {"Stripe-Signature": sig})
        self.assertEqual(a.db.one("SELECT sum(amount_cents)::int s FROM ledger WHERE order_id=%s AND party='artist'", (o3["id"],))["s"], 450)
        # Custom split rules per league are validated and applied
        promoter.get("/manage/la-synthetic/revenue")
        bad_split = {f"{k}_{p}": v for k in ("ticket", "ppv", "tip", "membership", "paid_media", "sponsor") for p, v in (("platform", "10"), ("league", "60"), ("artist", "0"), ("event_pool", "20"))}
        self.assertIn("100%", promoter.post("/manage/la-synthetic/splits", bad_split)["flash"]["text"])
        good = {**{f"{k}_{p}": v for k in ("ticket", "ppv", "sponsor") for p, v in (("platform", "5"), ("league", "65"), ("artist", "0"), ("event_pool", "30"))},
                **{f"{k}_{p}": v for k in ("tip", "membership", "paid_media") for p, v in (("platform", "5"), ("league", "5"), ("artist", "90"), ("event_pool", "0"))}}
        self.assertEqual(promoter.post("/manage/la-synthetic/splits", good)["flash"]["text"], "Revenue splits saved.")
        # Refund (partial) and chargeback reverse ledger lines; refund API call goes to Stripe
        promoter.post(f"/manage/la-synthetic/orders/{order['id']}/refund")
        self.assertTrue(self.stripe.calls[-1][0].endswith("/refunds"))
        payload, sig = signed("whsec_fake", {"id": "evt_4", "type": "charge.refunded", "data": {"object": {"payment_intent": "pi_1", "amount_refunded": 1000}}})
        Client(a).request("POST", "/stripe/webhook", payload, "application/json", {"Stripe-Signature": sig})
        self.assertEqual(a.db.one("SELECT status, refunded_cents FROM orders WHERE id=%s", (order["id"],)), {"status": "partially_refunded", "refunded_cents": 1000})
        payload, sig = signed("whsec_fake", {"id": "evt_5", "type": "charge.dispute.created", "data": {"object": {"payment_intent": "pi_2", "amount": 800}}})
        Client(a).request("POST", "/stripe/webhook", payload, "application/json", {"Stripe-Signature": sig})
        self.assertEqual(a.db.one("SELECT sum(amount_cents)::int s FROM ledger WHERE order_id=%s", (o2["id"],))["s"], 0)
        total = a.db.one("SELECT sum(amount_cents)::int s FROM ledger")["s"]
        self.assertEqual(total, 2000 - 1000 + 800 - 800 + 500)
        # Settlement: event pool divides between the two emcees; the one without payout consent is held
        s = promoter.post("/manage/la-synthetic/settle")
        self.assertIn("Settlement #", s["flash"]["text"])
        st = a.db.one("SELECT * FROM settlements ORDER BY id DESC LIMIT 1")
        self.assertEqual(st["entry_count"], a.db.one("SELECT count(*)::int n FROM ledger")["n"])
        totals = st["totals"]
        self.assertEqual(sum(totals["artists"].values()) + sum(totals["held"].values()) + totals["platform"] + totals["league"] + totals["event_pool_unallocated"], total)
        self.assertTrue(any("Flux Synth" in k for k in totals["held"]))
        self.assertTrue(any("Echo Synth" in k for k in totals["artists"]))
        self.assertEqual(len(st["evidence_sha256"]), 64)
        self.assertIn("Nothing to settle", promoter.post("/manage/la-synthetic/settle")["flash"]["text"])
        # Membership and paid media
        emc.get(f"/l/la-synthetic/artists/{emc.artist_id}/edit")
        emc.post(f"/l/la-synthetic/artists/{emc.artist_id}/media", {"title": "Synthetic unreleased verse", "url": "https://soundcloud.com/synthetic/verse", "price": "3"})
        media_id = a.db.one("SELECT id FROM paid_media")["id"]
        self.assertEqual(fan.get(f"/l/la-synthetic/media/{media_id}")["status"], 404)
        fan.post("/pay/checkout", {"kind": "paid_media", "media_id": str(media_id)})
        o4 = a.db.one("SELECT * FROM orders ORDER BY id DESC LIMIT 1")
        payload, sig = signed("whsec_fake", {"id": "evt_6", "type": "checkout.session.completed", "data": {"object": {"id": o4["stripe_session_id"], "payment_status": "paid", "amount_total": 300, "payment_intent": "pi_6", "metadata": {"order_id": str(o4["id"])}}}})
        Client(a).request("POST", "/stripe/webhook", payload, "application/json", {"Stripe-Signature": sig})
        self.assertEqual(fan.get(f"/l/la-synthetic/media/{media_id}")["status"], 200)
        self.assertIn("Purchases", fan.get("/account")["body"])
        # Mismatched amount is ignored (never trust a client-chosen total)
        fan.post("/pay/checkout", {"kind": "membership", "artist_id": str(emc.artist_id)})
        o5 = a.db.one("SELECT * FROM orders ORDER BY id DESC LIMIT 1")
        payload, sig = signed("whsec_fake", {"id": "evt_7", "type": "checkout.session.completed", "data": {"object": {"id": o5["stripe_session_id"], "payment_status": "paid", "amount_total": 1, "payment_intent": "pi_7", "metadata": {"order_id": str(o5["id"])}}}})
        self.assertEqual(json.loads(Client(a).request("POST", "/stripe/webhook", payload, "application/json", {"Stripe-Signature": sig})["body"])["result"], "ignored")
        self.assertEqual(a.db.one("SELECT status FROM orders WHERE id=%s", (o5["id"],))["status"], "pending")


if __name__ == "__main__":
    unittest.main()
