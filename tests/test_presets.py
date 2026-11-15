import unittest

from rapbattle.presets import (PRESETS, PRESET_IDS, Match, MatchError, bracket, default_config,
                               validate_config)


class PresetTests(unittest.TestCase):
    def test_all_required_templates_ship_with_valid_defaults(self):
        self.assertEqual(set(PRESET_IDS), {"one-v-one", "one-round", "three-round", "crew", "tournament"})
        for preset_id in PRESET_IDS:
            config = validate_config(preset_id, default_config(preset_id))
            self.assertIn(config["style"], ("written", "freestyle"))
            self.assertFalse(config["audience_vote_enabled"], "audience voting is optional and off by default")
        self.assertEqual(default_config("one-round")["rounds"], 1)
        self.assertEqual(default_config("three-round")["rounds"], 3)
        self.assertGreaterEqual(default_config("crew")["side_size"], 2)
        self.assertEqual(default_config("tournament")["bracket_size"], 8)

    def test_defaults_are_immutable_and_copies_are_independent(self):
        copy = default_config("one-v-one")
        copy["rounds"] = 5
        self.assertEqual(PRESETS["one-v-one"]["rounds"], 2)
        with self.assertRaises(TypeError):
            PRESETS["one-v-one"]["rounds"] = 9

    def test_invalid_configurations_are_rejected(self):
        bad = [
            ("one-round", {"rounds": 2}), ("three-round", {"rounds": 1}), ("crew", {"side_size": 1}),
            ("one-v-one", {"side_size": 2}), ("tournament", {"bracket_size": 6}),
            ("one-v-one", {"bracket_size": 8}), ("one-v-one", {"judges": 2}),
            ("one-v-one", {"round_seconds": 5}), ("one-v-one", {"round_seconds": True}),
            ("one-v-one", {"style": "<script>"}), ("one-v-one", {"tie_policy": "coin-flip"}),
            ("one-v-one", {"max_overtimes": 0}), ("one-v-one", {"audience_vote_enabled": "yes"}),
        ]
        for preset_id, change in bad:
            with self.subTest(preset=preset_id, change=change), self.assertRaises(ValueError):
                validate_config(preset_id, {**default_config(preset_id), **change})
        with self.assertRaises(ValueError):
            validate_config("one-v-one", {**default_config("one-v-one"), "extra": 1})
        with self.assertRaises(ValueError):
            validate_config("__proto__", default_config("one-v-one"))

    def play(self, match, rounds, seconds=60):
        for r in range(rounds):
            order = match.sides if (match.round % 2) else match.sides[::-1]
            for side in order:
                match.record_turn(side, seconds)

    def test_three_round_battle_with_timer_flags_and_judged_winner(self):
        m = Match("three-round", default_config("three-round"))
        m.start()
        m.record_turn("a", 95)  # 5 seconds over the 90 second timer
        m.record_turn("b", 80)
        with self.assertRaises(MatchError):
            m.record_turn("a", 10)  # round 2 alternates: b opens
        m.record_turn("b", 90)
        m.record_turn("a", 90)
        m.record_turn("a", 90)
        m.record_turn("b", 90)
        self.assertEqual(m.state, "judging")
        self.assertEqual(m.timer_flags, [(1, "a", 5)])
        self.assertEqual(m.judge(["a", "b", "a"]), "decided")
        self.assertEqual(m.winner, "a")
        with self.assertRaises(MatchError):
            m.forfeit("a")

    def test_tie_goes_to_overtime_then_draw_when_exhausted(self):
        m = Match("one-round", default_config("one-round"))
        m.start()
        self.play(m, 1)
        self.assertEqual(m.judge(["a", "b", "tie"]), "overtime")
        self.play(m, 1, seconds=70)  # overtime limit is 60s
        self.assertEqual(m.timer_flags[-1][2], 10)
        self.assertEqual(m.judge(["tie", "tie", "tie"]), "draw")
        self.assertIsNone(m.winner)

    def test_draw_and_recount_policies(self):
        cfg = {**default_config("one-v-one"), "tie_policy": "draw"}
        m = Match("one-v-one", cfg)
        m.start()
        self.play(m, 2)
        self.assertEqual(m.judge(["a", "b", "tie"]), "draw")
        crew = Match("crew", default_config("crew"))
        crew.start()
        self.play(crew, 3)
        self.assertEqual(crew.judge(["a", "a", "b", "b", "tie"]), "judging")
        self.assertEqual(crew.outcome_reason, "recount-required")
        self.assertEqual(crew.judge(["a", "a", "b", "b", "b"]), "decided")
        self.assertEqual(crew.winner, "b")

    def test_forfeit_cancel_and_invalid_ballots(self):
        m = Match("one-v-one", default_config("one-v-one"))
        with self.assertRaises(MatchError):
            m.judge(["a", "a", "a"])
        m.forfeit("b", "no-show")
        self.assertEqual((m.state, m.winner, m.outcome_reason), ("forfeited", "a", "no-show"))
        c = Match("one-v-one", default_config("one-v-one"))
        c.cancel()
        self.assertEqual(c.state, "cancelled")
        j = Match("one-v-one", default_config("one-v-one"))
        j.start()
        self.play(j, 2)
        for ballots in (["a", "a"], ["a", "a", "x"], "aaa"):
            with self.assertRaises(MatchError):
                j.judge(ballots)
        with self.assertRaises(MatchError):
            Match("one-v-one", default_config("one-v-one"), sides=("a", "a"))

    def test_tournament_bracket_seeding_and_byes(self):
        pairs = bracket(["s1", "s2", "s3", "s4", "s5", "s6"], 8)
        self.assertEqual(pairs[0], ("s1", None))
        self.assertEqual(pairs[1], ("s2", None))
        self.assertEqual(pairs[2], ("s3", "s6"))
        for bad in ((["x"], 8), (["x", "x"], 4), ([str(i) for i in range(5)], 4), (["a", "b"], 6)):
            with self.assertRaises(ValueError):
                bracket(*bad)


if __name__ == "__main__":
    unittest.main()
