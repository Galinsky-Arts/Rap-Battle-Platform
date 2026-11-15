"""RAP-04 battle presets and match lifecycle (downstream rules, engine-agnostic).

Presets are configurable templates for 1v1, one-round, three-round, crew and
tournament battles in written or freestyle style, with round timers and explicit
tie, overtime, draw and forfeit outcomes. They are league configuration; they do
not start live events, take payments or publish results by themselves.
"""
from copy import deepcopy
from types import MappingProxyType

STYLES = ("written", "freestyle")
TIE_POLICIES = ("overtime-round", "draw", "judges-recount")
KEYS = ("rounds", "round_seconds", "style", "side_size", "judges", "tie_policy",
        "overtime_seconds", "max_overtimes", "bracket_size", "audience_vote_enabled")

_DEFAULTS = {
    "one-v-one": {"label": "1v1 battle", "rounds": 2, "round_seconds": 90, "style": "written", "side_size": 1,
                  "judges": 3, "tie_policy": "overtime-round", "overtime_seconds": 60, "max_overtimes": 1,
                  "bracket_size": None, "audience_vote_enabled": False},
    "one-round": {"label": "One-round battle", "rounds": 1, "round_seconds": 120, "style": "freestyle", "side_size": 1,
                  "judges": 3, "tie_policy": "overtime-round", "overtime_seconds": 60, "max_overtimes": 1,
                  "bracket_size": None, "audience_vote_enabled": False},
    "three-round": {"label": "Three-round battle", "rounds": 3, "round_seconds": 90, "style": "written", "side_size": 1,
                    "judges": 3, "tie_policy": "overtime-round", "overtime_seconds": 60, "max_overtimes": 1,
                    "bracket_size": None, "audience_vote_enabled": False},
    "crew": {"label": "Crew battle", "rounds": 3, "round_seconds": 120, "style": "written", "side_size": 3,
             "judges": 5, "tie_policy": "judges-recount", "overtime_seconds": 0, "max_overtimes": 0,
             "bracket_size": None, "audience_vote_enabled": False},
    "tournament": {"label": "Tournament bracket (1v1 matches)", "rounds": 1, "round_seconds": 60, "style": "freestyle",
                   "side_size": 1, "judges": 3, "tie_policy": "overtime-round", "overtime_seconds": 45,
                   "max_overtimes": 2, "bracket_size": 8, "audience_vote_enabled": False},
}
PRESETS = MappingProxyType({k: MappingProxyType(v) for k, v in _DEFAULTS.items()})
PRESET_IDS = tuple(PRESETS)


def default_config(preset_id):
    if preset_id not in PRESETS:
        raise ValueError("Unknown battle preset")
    return {k: deepcopy(PRESETS[preset_id][k]) for k in KEYS}


def _int(value, low, high, label):
    if type(value) is not int or not low <= value <= high:
        raise ValueError(f"{label} must be an integer from {low} to {high}")
    return value


def validate_config(preset_id, config):
    if not isinstance(preset_id, str) or preset_id not in PRESETS:
        raise ValueError("Unknown battle preset")
    if not isinstance(config, dict) or set(config) != set(KEYS):
        raise ValueError("Preset configuration requires exactly: " + ", ".join(KEYS))
    c = {"rounds": _int(config["rounds"], 1, 5, "Rounds"),
         "round_seconds": _int(config["round_seconds"], 30, 600, "Round length"),
         "side_size": _int(config["side_size"], 1, 6, "Side size"),
         "judges": _int(config["judges"], 1, 9, "Judges"),
         "overtime_seconds": _int(config["overtime_seconds"], 0, 300, "Overtime length"),
         "max_overtimes": _int(config["max_overtimes"], 0, 3, "Overtime rounds")}
    if config["style"] not in STYLES:
        raise ValueError("Style must be written or freestyle")
    if config["tie_policy"] not in TIE_POLICIES:
        raise ValueError("Unsupported tie policy")
    if type(config["audience_vote_enabled"]) is not bool:
        raise ValueError("Audience vote flag must be boolean")
    c.update(style=config["style"], tie_policy=config["tie_policy"],
             audience_vote_enabled=config["audience_vote_enabled"], bracket_size=config["bracket_size"])
    if c["judges"] % 2 == 0:
        raise ValueError("Use an odd judge count so a panel majority exists")
    if c["tie_policy"] == "overtime-round" and (c["max_overtimes"] < 1 or c["overtime_seconds"] < 30):
        raise ValueError("Overtime tie policy needs at least one overtime of 30 seconds or more")
    if preset_id == "one-round" and c["rounds"] != 1:
        raise ValueError("One-round preset must have exactly one round")
    if preset_id == "three-round" and c["rounds"] != 3:
        raise ValueError("Three-round preset must have exactly three rounds")
    if preset_id == "crew":
        if c["side_size"] < 2:
            raise ValueError("Crew battles need at least two emcees per side")
    elif c["side_size"] != 1:
        raise ValueError("Only crew battles may have more than one emcee per side")
    if preset_id == "tournament":
        if c["bracket_size"] not in (4, 8, 16, 32):
            raise ValueError("Tournament bracket size must be 4, 8, 16 or 32")
    elif c["bracket_size"] is not None:
        raise ValueError("Only tournaments have a bracket size")
    return c


def bracket(entrants, size):
    """Seeded first-round pairs (1 vs N, 2 vs N-1, ...); missing seeds are byes (None)."""
    if size not in (4, 8, 16, 32) or not isinstance(entrants, list) or not 2 <= len(entrants) <= size:
        raise ValueError("Bracket needs 2..size entrants")
    if len(set(entrants)) != len(entrants):
        raise ValueError("Duplicate bracket entrant")
    seeds = entrants + [None] * (size - len(entrants))
    return [(seeds[i], seeds[size - 1 - i]) for i in range(size // 2)]


class MatchError(ValueError):
    pass


class Match:
    """Lifecycle for one battle under a validated preset.

    scheduled -> in_progress -> judging -> decided | overtime -> judging ... | draw
    scheduled/in_progress/judging/overtime -> forfeited ; scheduled -> cancelled
    """
    TERMINAL = ("decided", "draw", "forfeited", "cancelled")

    def __init__(self, preset_id, config, sides=("a", "b")):
        self.config = validate_config(preset_id, config)
        if len(sides) != 2 or len(set(sides)) != 2:
            raise MatchError("A battle has exactly two sides")
        self.sides = tuple(sides)
        self.state = "scheduled"
        self.round = 0
        self.overtimes = 0
        self.turns = []
        self.timer_flags = []
        self.winner = None
        self.outcome_reason = None

    def _need(self, *states):
        if self.state not in states:
            raise MatchError(f"Action not allowed while {self.state}")

    def start(self):
        self._need("scheduled")
        self.state, self.round = "in_progress", 1

    def _limit(self):
        return self.config["overtime_seconds"] if self.round > self.config["rounds"] else self.config["round_seconds"]

    def record_turn(self, side, elapsed_seconds):
        self._need("in_progress", "overtime")
        if side not in self.sides:
            raise MatchError("Unknown side")
        if type(elapsed_seconds) is not int or elapsed_seconds < 0:
            raise MatchError("Elapsed time must be a non-negative integer")
        current = [t for t in self.turns if t[0] == self.round]
        expected = self.sides[len(current) % 2] if self.round % 2 else self.sides[1 - len(current) % 2]
        if len(current) >= 2 or side != expected:
            raise MatchError("Out-of-order turn")
        self.turns.append((self.round, side, elapsed_seconds))
        if elapsed_seconds > self._limit():
            self.timer_flags.append((self.round, side, elapsed_seconds - self._limit()))
        if len(current) == 1:
            final_round = self.config["rounds"] + self.overtimes
            if self.round >= final_round:
                self.state = "judging"
            else:
                self.round += 1

    def judge(self, ballots):
        """ballots: one side name or 'tie' per judge, exactly config['judges'] entries."""
        self._need("judging")
        if not isinstance(ballots, list) or len(ballots) != self.config["judges"] or any(
                b not in (*self.sides, "tie") for b in ballots):
            raise MatchError("Exactly one valid ballot per judge is required")
        a, b = (ballots.count(s) for s in self.sides)
        if a != b:
            self.state, self.winner, self.outcome_reason = "decided", self.sides[0] if a > b else self.sides[1], "judges"
            return self.state
        policy = self.config["tie_policy"]
        if policy == "overtime-round" and self.overtimes < self.config["max_overtimes"]:
            self.overtimes += 1
            self.round += 1
            self.state = "overtime"
        elif policy == "judges-recount":
            # Judges must re-ballot without the 'tie' option being decisive again.
            self.state = "judging"
            self.outcome_reason = "recount-required"
        else:
            self.state, self.outcome_reason = "draw", "tie"
        return self.state

    def forfeit(self, side, reason="forfeit"):
        self._need("scheduled", "in_progress", "overtime", "judging")
        if side not in self.sides:
            raise MatchError("Unknown side")
        self.state = "forfeited"
        self.winner = self.sides[1] if side == self.sides[0] else self.sides[0]
        self.outcome_reason = reason if reason in ("forfeit", "no-show", "disqualified") else "forfeit"

    def cancel(self):
        self._need("scheduled")
        self.state = "cancelled"
