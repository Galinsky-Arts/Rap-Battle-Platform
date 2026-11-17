"""Built-in battle engine on Postgres implementing the CompetitionEngine adapter contract.

Events (with preset snapshot), rubric criteria, entrants, judges, matches with per-round turns
and timers, rubric scoring per judge/round/side, audience votes with anti-abuse limits, tie
policies (overtime / draw / judges-recount), forfeits, seeded single-elimination brackets with
byes, results, champions, disputes and corrections.
"""
from datetime import datetime, timezone

from psycopg.types.json import Jsonb

from .. import presets as preset_rules
from ..web.http import HttpError
from ..web.db import UniqueViolation
from . import scoring

DEFAULT_CRITERIA = (("Lyricism", 3000, 10), ("Delivery", 2500, 10), ("Punchlines", 2500, 10), ("Crowd response", 2000, 10))


def conflict(msg):
    return HttpError(409, msg)


class BuiltinEngine:
    kind = "builtin"

    def __init__(self, db):
        self.db = db

    def health(self):
        self.db.one("SELECT 1")
        return {"connected": True, "kind": "builtin"}

    # ---------- events ----------
    def list_events(self, league=None, *, published_only=False, statuses=None, limit=200):
        cond, p = [], []
        if league:
            cond.append("league_slug=%s")
            p.append(league)
        if published_only:
            cond.append("published")
        if statuses:
            cond.append("status = ANY(%s)")
            p.append(list(statuses))
        where = ("WHERE " + " AND ".join(cond)) if cond else ""
        return self.db.all(f"SELECT * FROM events {where} ORDER BY COALESCE(starts_at, created_at) DESC, id DESC LIMIT {int(limit)}", p)

    def event(self, event_id, league=None):
        e = self.db.one("SELECT * FROM events WHERE id=%s", (event_id,))
        if not e or (league and e["league_slug"] != league):
            raise HttpError(404, "Event not found")
        return e

    def event_by_slug(self, league, slug):
        return self.db.one("SELECT * FROM events WHERE league_slug=%s AND slug=%s", (league, slug))

    def create_event(self, league, *, slug, title, preset_id, config, season_id=None, starts_at=None, venue="", description="", synthetic=False):
        config = preset_rules.validate_config(preset_id, config)
        try:
            with self.db.tx() as q:
                e = q.one("""INSERT INTO events(league_slug,season_id,slug,title,description,venue,starts_at,preset_id,config,synthetic)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *""", (league, season_id, slug, title, description, venue, starts_at, preset_id, Jsonb(config), synthetic))
                for i, (name, w, mx) in enumerate(DEFAULT_CRITERIA):
                    q.run("INSERT INTO criteria(event_id,name,weight_bps,max_score,position) VALUES (%s,%s,%s,%s,%s)", (e["id"], name, w, mx, i))
                return e
        except UniqueViolation:
            raise conflict("An event with that slug already exists in this league")

    def update_event(self, event_id, **fields):
        e = self.event(event_id)
        allowed = {"title", "description", "venue", "starts_at", "status", "published", "voting_enabled", "votes_per_fan", "stream_url", "ticket_cents", "ppv_cents", "season_id"}
        f = {k: v for k, v in fields.items() if k in allowed}
        if f.get("status") == "completed" and not e["finalized_at"]:
            raise conflict("Finalize the event to complete it")
        if f.get("status") and f["status"] not in ("draft", "scheduled", "live", "completed", "cancelled"):
            raise HttpError(400, "Invalid status")
        if not f:
            return e
        sets = ", ".join(f"{k}=%s" for k in f)
        return self.db.one(f"UPDATE events SET {sets} WHERE id=%s RETURNING *", (*f.values(), event_id))

    def criteria(self, event_id):
        return self.db.all("SELECT * FROM criteria WHERE event_id=%s ORDER BY position, id", (event_id,))

    def add_criterion(self, event_id, name, weight_bps, max_score):
        return self.db.one("""INSERT INTO criteria(event_id,name,weight_bps,max_score,position)
            VALUES (%s,%s,%s,%s,(SELECT COALESCE(max(position),0)+1 FROM criteria WHERE event_id=%s)) RETURNING *""", (event_id, name, weight_bps, max_score, event_id))

    def remove_criterion(self, event_id, criterion_id):
        if self.db.one("SELECT 1 FROM scores WHERE criterion_id=%s LIMIT 1", (criterion_id,)):
            raise conflict("Criterion already has scores")
        self.db.run("DELETE FROM criteria WHERE id=%s AND event_id=%s", (criterion_id, event_id))

    # ---------- entrants & judges ----------
    def entrants(self, event_id):
        return self.db.all("SELECT * FROM entrants WHERE event_id=%s ORDER BY seed NULLS LAST, id", (event_id,))

    def add_entrant(self, event_id, display_name, artist_id=None, seed=None):
        try:
            return self.db.one("INSERT INTO entrants(event_id,artist_id,display_name,seed) VALUES (%s,%s,%s,%s) RETURNING *", (event_id, artist_id, display_name, seed))
        except UniqueViolation:
            raise conflict("That artist is already on the lineup")

    def set_entrant(self, entrant_id, *, status=None, seed=None):
        if status and status not in ("active", "withdrawn", "disqualified"):
            raise HttpError(400, "Invalid status")
        self.db.run("UPDATE entrants SET status=COALESCE(%s,status), seed=%s WHERE id=%s", (status, seed, entrant_id))

    def judges(self, event_id):
        return self.db.all("SELECT u.id, u.display_name, u.email FROM judges j JOIN users u ON u.id=j.user_id WHERE j.event_id=%s ORDER BY u.display_name", (event_id,))

    def assign_judge(self, event_id, user_id):
        e = self.event(event_id)
        if not self.db.one("SELECT 1 FROM league_members WHERE league_slug=%s AND user_id=%s AND role='judge'", (e["league_slug"], user_id)):
            raise HttpError(400, "That account is not a judge in this league")
        self.db.run("INSERT INTO judges(event_id,user_id) VALUES (%s,%s) ON CONFLICT DO NOTHING", (event_id, user_id))

    def remove_judge(self, event_id, user_id):
        self.db.run("DELETE FROM judges WHERE event_id=%s AND user_id=%s", (event_id, user_id))

    def is_judge(self, event_id, user_id):
        return bool(self.db.one("SELECT 1 FROM judges WHERE event_id=%s AND user_id=%s", (event_id, user_id)))

    def judge_events(self, user_id):
        return self.db.all("SELECT e.* FROM events e JOIN judges j ON j.event_id=e.id WHERE j.user_id=%s ORDER BY e.id DESC", (user_id,))

    # ---------- matches ----------
    def matches(self, event_id):
        return self.db.all("""SELECT m.*, ea.display_name AS a_name, eb.display_name AS b_name, ea.artist_id AS a_artist, eb.artist_id AS b_artist
            FROM matches m LEFT JOIN entrants ea ON ea.id=m.side_a LEFT JOIN entrants eb ON eb.id=m.side_b WHERE m.event_id=%s ORDER BY m.stage, m.position""", (event_id,))

    def match(self, match_id, league=None):
        m = self.db.one("""SELECT m.*, e.league_slug, e.config, e.voting_enabled, e.votes_per_fan, e.published, e.title AS event_title, e.slug AS event_slug,
            ea.display_name AS a_name, eb.display_name AS b_name, ea.artist_id AS a_artist, eb.artist_id AS b_artist
            FROM matches m JOIN events e ON e.id=m.event_id LEFT JOIN entrants ea ON ea.id=m.side_a LEFT JOIN entrants eb ON eb.id=m.side_b WHERE m.id=%s""", (match_id,))
        if not m or (league and m["league_slug"] != league):
            raise HttpError(404, "Battle not found")
        return m

    def add_match(self, event_id, side_a, side_b):
        if side_a == side_b:
            raise HttpError(400, "Pick two different emcees")
        ids = {e["id"] for e in self.entrants(event_id) if e["status"] == "active"}
        if side_a not in ids or side_b not in ids:
            raise HttpError(400, "Both emcees must be active on this event's lineup")
        return self.db.one("""INSERT INTO matches(event_id,stage,position,side_a,side_b)
            VALUES (%s,1,(SELECT COALESCE(max(position),0)+1 FROM matches WHERE event_id=%s AND stage=1),%s,%s) RETURNING *""", (event_id, event_id, side_a, side_b))

    def generate_bracket(self, event_id):
        e = self.event(event_id)
        if self.db.one("SELECT 1 FROM matches WHERE event_id=%s LIMIT 1", (event_id,)):
            raise conflict("Battles already exist for this event")
        seeds = [x["id"] for x in self.entrants(event_id) if x["status"] == "active"]
        if len(seeds) < 2:
            raise conflict("A bracket needs at least two active emcees")
        cap = e["config"].get("bracket_size")
        if cap and len(seeds) > cap:
            raise conflict(f"The bracket holds {cap} emcees; withdraw some or raise the bracket size")
        size = scoring.bracket_size(len(seeds))
        if size > 32:
            raise conflict("Brackets support up to 32 emcees")
        pairs = [(seeds[0], seeds[1])] if size == 2 else preset_rules.bracket(seeds, size)
        with self.db.tx() as q:
            stages, count, stage = [], len(pairs), 1
            while count >= 1:
                stages.append([q.one("INSERT INTO matches(event_id,stage,position) VALUES (%s,%s,%s) RETURNING id", (event_id, stage, i + 1))["id"] for i in range(count)])
                if count == 1:
                    break
                count //= 2
                stage += 1
            for s in range(len(stages) - 1):
                for i, mid in enumerate(stages[s]):
                    q.run("UPDATE matches SET next_match_id=%s, next_slot=%s WHERE id=%s", (stages[s + 1][i // 2], "a" if i % 2 == 0 else "b", mid))
            for i, (a, b) in enumerate(pairs):
                q.run("UPDATE matches SET side_a=%s, side_b=%s WHERE id=%s", (a, b, stages[0][i]))
        for mid in stages[0]:
            m = self.match(mid)
            if (m["side_a"] is None) != (m["side_b"] is None):
                self._finish(mid, "decided", "a" if m["side_a"] else "b", "bye")
        return len(stages)

    def _finish(self, match_id, status, winner_side, reason, q=None):
        db = q or self.db
        m = db.one("SELECT * FROM matches WHERE id=%s", (match_id,))
        winner = m["side_" + winner_side] if winner_side else None
        db.run("""UPDATE matches SET status=%s, winner=%s, outcome_reason=%s, voting_open=false, timer_running=false, timer_side=NULL,
            timer_started_at=NULL, decided_at=now() WHERE id=%s""", (status, winner, reason, match_id))
        if winner and m["next_match_id"]:
            db.run(f"UPDATE matches SET side_{m['next_slot']}=%s WHERE id=%s", (winner, m["next_match_id"]))

    def _limit(self, m):
        cfg = m["config"]
        return cfg["overtime_seconds"] if m["current_round"] > cfg["rounds"] else cfg["round_seconds"]

    def start_match(self, match_id):
        m = self.match(match_id)
        if m["status"] != "scheduled":
            raise conflict("Battle already started")
        if not (m["side_a"] and m["side_b"]):
            raise conflict("Both sides must be set")
        self.db.run("UPDATE matches SET status='in_progress', current_round=1 WHERE id=%s", (match_id,))
        self.db.run("UPDATE events SET status='live' WHERE id=%s AND status IN ('draft','scheduled')", (m["event_id"],))

    def expected_side(self, m):
        done = {t["side"] for t in self.db.all("SELECT side FROM turns WHERE match_id=%s AND round=%s", (m["id"], m["current_round"]))}
        for side in scoring.turn_order(m["current_round"]):
            if side not in done:
                return side
        return None

    def timer(self, match_id, action):
        with self.db.tx() as q:
            m = q.one("SELECT m.*, e.config FROM matches m JOIN events e ON e.id=m.event_id WHERE m.id=%s FOR UPDATE OF m", (match_id,))
            if not m:
                raise HttpError(404, "Battle not found")
            if m["status"] not in ("in_progress", "overtime"):
                raise conflict("The battle is not in a performing round")
            st = scoring.timer_state(m, self._limit(m))
            if action == "start":
                if m["timer_side"]:
                    raise conflict("Stop the current turn first")
                side = self.expected_side(m)
                if not side:
                    raise conflict("Both turns in this round are recorded")
                q.run("UPDATE matches SET timer_side=%s, timer_started_at=now(), timer_elapsed_ms=0, timer_running=true WHERE id=%s", (side, match_id))
            elif action == "pause":
                if not m["timer_running"]:
                    raise conflict("Timer is not running")
                q.run("UPDATE matches SET timer_elapsed_ms=%s, timer_running=false, timer_started_at=NULL WHERE id=%s", (st["elapsed_ms"], match_id))
            elif action == "resume":
                if m["timer_running"] or not m["timer_side"]:
                    raise conflict("Nothing to resume")
                q.run("UPDATE matches SET timer_running=true, timer_started_at=now() WHERE id=%s", (match_id,))
            elif action == "stop":
                if not m["timer_side"]:
                    raise conflict("No turn in progress")
                q.run("INSERT INTO turns(match_id,round,side,elapsed_ms,over_ms) VALUES (%s,%s,%s,%s,%s)", (match_id, m["current_round"], m["timer_side"], st["elapsed_ms"], st["over_ms"]))
                q.run("UPDATE matches SET timer_side=NULL, timer_running=false, timer_started_at=NULL, timer_elapsed_ms=0 WHERE id=%s", (match_id,))
                n = q.one("SELECT count(*) AS n FROM turns WHERE match_id=%s AND round=%s", (match_id, m["current_round"]))["n"]
                if n == 2:
                    final_round = m["config"]["rounds"] + m["overtimes"]
                    if m["current_round"] >= final_round:
                        q.run("UPDATE matches SET status='judging' WHERE id=%s", (match_id,))
                    else:
                        q.run("UPDATE matches SET current_round=current_round+1 WHERE id=%s", (match_id,))
            else:
                raise HttpError(400, "Unknown timer action")
            return st

    def turns(self, match_id):
        return self.db.all("SELECT * FROM turns WHERE match_id=%s ORDER BY round, side", (match_id,))

    # ---------- judging ----------
    def scores_for(self, match_id, judge_id):
        return self.db.all("SELECT * FROM scores WHERE match_id=%s AND judge_user_id=%s", (match_id, judge_id))

    def submit_scores(self, judge_id, match_id, values):
        """values: {(round, side, criterion_id): value}."""
        m = self.match(match_id)
        if not self.is_judge(m["event_id"], judge_id):
            raise HttpError(403, "You are not a judge for this event")
        if m["status"] not in ("in_progress", "judging", "overtime"):
            raise conflict("Scores can only be entered during the battle")
        crit = {c["id"]: c for c in self.criteria(m["event_id"])}
        max_round = max(m["current_round"], 1)
        with self.db.tx() as q:
            for (rnd, side, cid), v in values.items():
                c = crit.get(cid)
                if not c or side not in ("a", "b") or not 1 <= rnd <= max_round:
                    raise HttpError(400, "Invalid score target")
                try:
                    n = float(v)
                except ValueError:
                    raise HttpError(400, f"{c['name']} must be a number")
                if not 0 <= n <= c["max_score"]:
                    raise HttpError(400, f"{c['name']} must be between 0 and {c['max_score']}")
                q.run("""INSERT INTO scores(match_id,judge_user_id,round,side,criterion_id,value) VALUES (%s,%s,%s,%s,%s,%s)
                    ON CONFLICT (match_id,judge_user_id,round,side,criterion_id) DO UPDATE SET value=EXCLUDED.value""", (match_id, judge_id, rnd, side, cid, n))

    # ---------- audience voting ----------
    def set_voting(self, match_id, open_):
        m = self.match(match_id)
        if open_ and not (m["voting_enabled"] and m["published"]):
            raise conflict("Audience voting is disabled or the event is unpublished")
        if open_ and m["status"] not in ("in_progress", "judging", "overtime"):
            raise conflict("Voting opens only during a live battle")
        self.db.run("UPDATE matches SET voting_open=%s WHERE id=%s", (open_, match_id))

    def open_votes(self, league=None):
        p = [league] if league else []
        return self.db.all(f"""SELECT m.id FROM matches m JOIN events e ON e.id=m.event_id WHERE m.voting_open AND e.voting_enabled AND e.published
            {"AND e.league_slug=%s" if league else ""} ORDER BY m.id""", p)

    def cast_vote(self, *, user, match_id, side, ip_hash, min_account_minutes=0, ip_cap=25):
        if side not in ("a", "b"):
            raise HttpError(400, "Pick a side")
        with self.db.tx() as q:
            m = q.one("""SELECT m.*, e.voting_enabled, e.published FROM matches m JOIN events e ON e.id=m.event_id WHERE m.id=%s FOR UPDATE OF m""", (match_id,))
            if not m or not (m["voting_open"] and m["voting_enabled"] and m["published"]):
                raise conflict("Voting is not open for this battle")
            age = (datetime.now(timezone.utc) - user.created_at).total_seconds() / 60
            if age < min_account_minutes:
                raise HttpError(403, f"Accounts must be at least {min_account_minutes} minutes old to vote")
            own = q.one("SELECT 1 FROM entrants en JOIN artists a ON a.id=en.artist_id WHERE en.id IN (%s,%s) AND a.user_id=%s", (m["side_a"], m["side_b"], user.id))
            if own:
                raise HttpError(403, "Emcees cannot vote in their own battle")
            if q.one("SELECT 1 FROM judges WHERE event_id=%s AND user_id=%s", (m["event_id"], user.id)):
                raise HttpError(403, "Judges cannot cast audience votes")
            if q.one("SELECT count(*) AS n FROM votes WHERE match_id=%s AND ip_hash=%s", (match_id, ip_hash))["n"] >= ip_cap:
                raise HttpError(429, "Too many votes from this network for this battle")
            try:
                q.run("INSERT INTO votes(match_id,side,user_id,ip_hash) VALUES (%s,%s,%s,%s)", (match_id, side, user.id, ip_hash))
            except UniqueViolation:
                raise conflict("You already voted in this battle")

    def vote_counts(self, match_id):
        rows = self.db.all("SELECT side, count(*)::int AS n FROM votes WHERE match_id=%s GROUP BY side", (match_id,))
        out = {"a": 0, "b": 0}
        out.update({r["side"]: r["n"] for r in rows})
        return out

    # ---------- decisions ----------
    def close_match(self, match_id):
        m = self.match(match_id)
        if m["status"] != "judging":
            raise conflict("Finish every turn before closing the battle")
        cfg = m["config"]
        rounds = set(range(cfg["rounds"] + 1, m["current_round"] + 1)) if m["overtimes"] else set(range(1, cfg["rounds"] + 1))
        scores = self.db.all("SELECT * FROM scores WHERE match_id=%s", (match_id,))
        ballots, totals = scoring.judge_ballots(scores, self.criteria(m["event_id"]), rounds)
        if not ballots:
            raise conflict("No judge has scored this battle")
        audience = None
        if m["voting_enabled"] and cfg.get("audience_vote_enabled"):
            audience, _, _ = scoring.audience_ballot(self.db.all("SELECT side FROM votes WHERE match_id=%s", (match_id,)))
        outcome, side = scoring.decide(list(ballots.values()), tie_policy=cfg["tie_policy"], overtimes=m["overtimes"], max_overtimes=cfg["max_overtimes"], audience=audience)
        with self.db.tx() as q:
            if outcome == "decided":
                self._finish(match_id, "decided", side, "judges", q)
            elif outcome == "overtime":
                q.run("UPDATE matches SET status='overtime', overtimes=overtimes+1, current_round=current_round+1 WHERE id=%s", (match_id,))
            elif outcome == "recount":
                q.run("DELETE FROM scores WHERE match_id=%s", (match_id,))
                q.run("UPDATE matches SET outcome_reason='recount-required' WHERE id=%s", (match_id,))
            else:
                self._finish(match_id, "draw", None, "tie", q)
        return {"outcome": outcome, "winner": side, "ballots": ballots, "audience": audience, "totals": totals}

    def forfeit(self, match_id, side, reason):
        m = self.match(match_id)
        if m["status"] in ("decided", "draw", "forfeited", "cancelled"):
            raise conflict("Battle is already over")
        if side not in ("a", "b") or not m["side_" + side]:
            raise HttpError(400, "Pick the side that forfeits")
        reason = reason if reason in ("forfeit", "no-show", "disqualified") else "forfeit"
        self._finish(match_id, "forfeited", "b" if side == "a" else "a", reason)

    def cancel(self, match_id):
        m = self.match(match_id)
        if m["status"] != "scheduled":
            raise conflict("Only scheduled battles can be cancelled")
        self.db.run("UPDATE matches SET status='cancelled' WHERE id=%s", (match_id,))

    def correct(self, match_id, winner_side):
        """Producer correction (dispute outcome): sets a new decision; downstream slots follow when unplayed."""
        m = self.match(match_id)
        if m["status"] not in ("decided", "draw", "forfeited"):
            raise conflict("Only finished battles can be corrected")
        if m["next_match_id"]:
            nxt = self.match(m["next_match_id"])
            if nxt["status"] != "scheduled":
                raise conflict("The next battle already started; correct it first")
        with self.db.tx() as q:
            if winner_side == "draw":
                q.run("UPDATE matches SET status='draw', winner=NULL, outcome_reason='corrected' WHERE id=%s", (match_id,))
                if m["next_match_id"]:
                    q.run(f"UPDATE matches SET side_{m['next_slot']}=NULL WHERE id=%s", (m["next_match_id"],))
            else:
                self._finish(match_id, "decided", winner_side, "corrected", q)

    def set_recording(self, match_id, url):
        self.db.run("UPDATE matches SET recording_url=%s WHERE id=%s", (url, match_id))

    def finalize_event(self, event_id):
        e = self.event(event_id)
        ms = self.matches(event_id)
        if not ms:
            raise conflict("The event has no battles")
        if any(m["status"] not in ("decided", "draw", "forfeited", "cancelled") for m in ms):
            raise conflict("Every battle must be finished first")
        last = max(m["stage"] for m in ms)
        finals = [m for m in ms if m["stage"] == last]
        champion = finals[0]["winner"] if len(finals) == 1 and last > 1 else None
        if len(finals) == 1 and last == 1 and len(ms) == 1:
            champion = finals[0]["winner"]
        return self.db.one("UPDATE events SET status='completed', finalized_at=now(), champion_entrant_id=%s WHERE id=%s RETURNING *", (champion, e["id"]))

    # ---------- records ----------
    def league_results(self, league, season_id=None):
        p = [league]
        cond = ""
        if season_id:
            cond = "AND e.season_id=%s"
            p.append(season_id)
        return self.db.all(f"""SELECT m.id, m.status, m.outcome_reason, e.title AS event_title, e.slug AS event_slug, e.preset_id, e.config->>'style' AS style, m.decided_at,
            ea.artist_id AS a, eb.artist_id AS b, ea.display_name AS a_name, eb.display_name AS b_name,
            CASE WHEN m.winner=m.side_a THEN ea.artist_id WHEN m.winner=m.side_b THEN eb.artist_id END AS winner
            FROM matches m JOIN events e ON e.id=m.event_id LEFT JOIN entrants ea ON ea.id=m.side_a LEFT JOIN entrants eb ON eb.id=m.side_b
            WHERE e.league_slug=%s AND e.published AND m.status IN ('decided','draw','forfeited') AND m.outcome_reason<>'bye' {cond} ORDER BY m.decided_at DESC NULLS LAST""", p)

    def standings(self, league, season_id=None):
        rows = [r for r in self.league_results(league, season_id) if r["a"] and r["b"]]
        pts = {"win": 3, "draw": 1, "loss": 0}
        if season_id:
            s = self.db.one("SELECT * FROM seasons WHERE id=%s", (season_id,))
            if s:
                pts = {"win": s["points_win"], "draw": s["points_draw"], "loss": s["points_loss"]}
        return scoring.standings(rows, **pts)

    def champions(self, league):
        return self.db.all("""SELECT e.title, e.slug, e.finalized_at, en.display_name FROM events e JOIN entrants en ON en.id=e.champion_entrant_id
            WHERE e.league_slug=%s AND e.published ORDER BY e.finalized_at DESC""", (league,))


ENGINE_METHODS = tuple(sorted(n for n in dir(BuiltinEngine) if not n.startswith("_") and callable(getattr(BuiltinEngine, n))))


class ExternalEngine:
    """Placeholder for a Live-Competition-Engine client: every operation fails closed."""
    kind = "external"

    def __init__(self, origin):
        self.origin = origin

    def health(self):
        return {"connected": False, "kind": "external"}

    def __getattr__(self, name):
        if name in ENGINE_METHODS:
            def unavailable(*a, **k):
                raise HttpError(503, "The external competition engine client is not implemented for this deployment")
            return unavailable
        raise AttributeError(name)


def select_engine(kind, db, origin=None):
    if kind == "external":
        return ExternalEngine(origin)
    if kind != "builtin":
        raise ValueError("RAP_ENGINE must be builtin or external")
    return BuiltinEngine(db)


def assert_engine(engine):
    missing = [m for m in ENGINE_METHODS if not callable(getattr(engine, m, None))]
    if missing:
        raise TypeError("Engine adapter is missing: " + ", ".join(missing))
    return engine

