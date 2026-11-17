"""Pure engine rules: ballots, tie policies, standings, timers, splits, Stripe signatures, adapters."""
import hashlib
import hmac
import json
import time
import unittest
from datetime import datetime, timedelta, timezone

from rapbattle.engine import scoring
from rapbattle.engine.builtin import BuiltinEngine, ExternalEngine, ENGINE_METHODS, assert_engine, select_engine
from rapbattle.payments import Stripe, DEFAULT_SPLITS
from rapbattle.web.http import HttpError, Markup, h, parse_multipart
from rapbattle.web.media import media_link, embed_url, sniff_image, validate_image
from rapbattle.web.settings import privacy_gaps

CRIT = [{"id": 1, "weight_bps": 6000, "max_score": 10}, {"id": 2, "weight_bps": 4000, "max_score": 10}]


def sc(judge, rnd, side, cid, v):
    return {"judge_user_id": judge, "round": rnd, "side": side, "criterion_id": cid, "value": v}


class ScoringTests(unittest.TestCase):
    def test_ballots_use_weighted_rubric_and_only_counted_rounds(self):
        scores = [sc(1, 1, "a", 1, 10), sc(1, 1, "a", 2, 0), sc(1, 1, "b", 1, 0), sc(1, 1, "b", 2, 10),
                  sc(2, 1, "a", 1, 5), sc(2, 1, "b", 1, 5), sc(2, 2, "b", 1, 9)]
        ballots, totals = scoring.judge_ballots(scores, CRIT, {1})
        self.assertEqual(ballots, {1: "a", 2: "tie"})
        self.assertEqual(totals[1], {"a": 60.0, "b": 40.0})
        ballots, _ = scoring.judge_ballots(scores, CRIT, {2})
        self.assertEqual(ballots, {2: "b"})

    def test_decide_majority_and_tie_policies(self):
        self.assertEqual(scoring.decide(["a", "a", "b"], tie_policy="draw", overtimes=0, max_overtimes=0), ("decided", "a"))
        self.assertEqual(scoring.decide(["a", "b"], tie_policy="overtime-round", overtimes=0, max_overtimes=1), ("overtime", None))
        self.assertEqual(scoring.decide(["a", "b"], tie_policy="overtime-round", overtimes=1, max_overtimes=1), ("draw", None))
        self.assertEqual(scoring.decide(["a", "b"], tie_policy="judges-recount", overtimes=0, max_overtimes=0), ("recount", None))
        self.assertEqual(scoring.decide(["a", "b", "tie"], tie_policy="draw", overtimes=0, max_overtimes=0, audience="b"), ("decided", "b"))
        self.assertEqual(scoring.audience_ballot([{"side": "a"}, {"side": "b"}])[0], None)

    def test_standings_points_and_order(self):
        rows = [{"a": 1, "b": 2, "a_name": "A", "b_name": "B", "winner": 1, "status": "decided"},
                {"a": 2, "b": 3, "a_name": "B", "b_name": "C", "winner": None, "status": "draw"},
                {"a": 3, "b": 1, "a_name": "C", "b_name": "A", "winner": 1, "status": "forfeited"},
                {"a": 1, "b": None, "a_name": "A", "b_name": None, "winner": 1, "status": "decided"}]
        st = scoring.standings(rows, win=3, draw=1, loss=0)
        self.assertEqual([(r["name"], r["points"], r["played"]) for r in st], [("A", 6, 2), ("B", 1, 2), ("C", 1, 2)])

    def test_turn_order_timer_bracket_size_and_split(self):
        self.assertEqual(scoring.turn_order(1), ("a", "b"))
        self.assertEqual(scoring.turn_order(2), ("b", "a"))
        now = datetime(2026, 1, 1, tzinfo=timezone.utc)
        t = scoring.timer_state({"timer_running": True, "timer_started_at": now - timedelta(seconds=70), "timer_elapsed_ms": 0}, 60, now)
        self.assertEqual((t["over_ms"], t["running"]), (10000, True))
        self.assertEqual([scoring.bracket_size(n) for n in (2, 3, 5, 8, 9)], [2, 4, 8, 8, 16])
        rule = {"platform_bps": 1000, "league_bps": 6000, "artist_bps": 0, "event_pool_bps": 3000}
        self.assertEqual(scoring.split(999, rule), {"platform": 99, "league": 601, "artist": 0, "event_pool": 299})
        self.assertTrue(all(sum(v) == 10000 for v in DEFAULT_SPLITS.values()))


class StripeTests(unittest.TestCase):
    def test_signature_verification(self):
        s = Stripe("sk_test_x", "whsec_abc")
        payload = json.dumps({"id": "evt"}).encode()
        t = int(time.time())
        sig = hmac.new(b"whsec_abc", f"{t}.".encode() + payload, hashlib.sha256).hexdigest()
        self.assertEqual(s.verify(payload, f"t={t},v1={sig}")["id"], "evt")
        for bad in (f"t={t},v1={'0' * 64}", None, f"t={t - 1000},v1={sig}"):
            with self.assertRaises(HttpError):
                s.verify(payload, bad)
        with self.assertRaises(HttpError):
            s.verify(payload, f"t={t - 1000},v1=" + hmac.new(b"whsec_abc", f"{t - 1000}.".encode() + payload, hashlib.sha256).hexdigest())

    def test_payments_off_without_both_keys(self):
        self.assertFalse(Stripe(None, None).enabled)
        self.assertFalse(Stripe("sk_test_x", None).enabled)
        self.assertFalse(Stripe("nonsense", "whsec_x").enabled)
        self.assertTrue(Stripe("sk_live_x", "whsec_x").enabled)


class WebPrimitiveTests(unittest.TestCase):
    def test_markup_concatenation_escapes_untrusted(self):
        out = Markup("<p>") + "<script>" + Markup("</p>")
        self.assertIsInstance(out, Markup)
        self.assertEqual(out, "<p>&lt;script&gt;</p>")
        self.assertEqual("<b>" + Markup("<i>"), "&lt;b&gt;<i>")
        self.assertEqual(h(None), "")

    def test_media_rules(self):
        self.assertIsNone(media_link(""))
        for bad in ("http://youtu.be/abcdefghijk", "https://evil.example/x", "javascript:alert(1)"):
            with self.assertRaises(HttpError):
                media_link(bad)
        self.assertEqual(embed_url("https://www.youtube.com/watch?v=abcdefghijk"), "https://www.youtube-nocookie.com/embed/abcdefghijk")
        self.assertEqual(embed_url("https://vimeo.com/123456789"), "https://player.vimeo.com/video/123456789")
        self.assertTrue(embed_url("https://soundcloud.com/a/b").startswith("https://w.soundcloud.com/player/?url="))
        self.assertEqual(sniff_image(b"\xff\xd8\xff\xe0"), "image/jpeg")
        with self.assertRaises(HttpError):
            validate_image({"data": b"GIF89a"}, 1000)
        with self.assertRaises(HttpError):
            validate_image({"data": b"\x89PNG\r\n\x1a\n" + b"0" * 2000}, 1000)

    def test_privacy_gate_and_multipart(self):
        self.assertEqual(privacy_gaps({}), ["operator name", "privacy contact", "retention days", "policy version", "privacy policy text"])
        self.assertEqual(privacy_gaps({"operator_name": "o", "privacy_contact": "c", "retention_days": 30, "policy_version": "v1", "policy_text": "t"}), [])
        body = b"--x\r\nContent-Disposition: form-data; name=\"a\"\r\n\r\n1\r\n--x\r\nContent-Disposition: form-data; name=\"f\"; filename=\"p.png\"\r\n\r\nDATA\r\n--x--\r\n"
        fields, files = parse_multipart(body, "multipart/form-data; boundary=x")
        self.assertEqual((fields, files["f"]["data"]), ({"a": "1"}, b"DATA"))

    def test_engine_adapters(self):
        self.assertIs(assert_engine(BuiltinEngine(None)).kind, "builtin")
        ext = select_engine("external", None, "https://engine.example")
        self.assertIsInstance(ext, ExternalEngine)
        self.assertEqual(ext.health(), {"connected": False, "kind": "external"})
        with self.assertRaises(HttpError):
            ext.cast_vote(user=None, match_id=1, side="a", ip_hash="x")
        assert_engine(ext)
        self.assertIn("close_match", ENGINE_METHODS)
        with self.assertRaises(ValueError):
            select_engine("other", None)


if __name__ == "__main__":
    unittest.main()
