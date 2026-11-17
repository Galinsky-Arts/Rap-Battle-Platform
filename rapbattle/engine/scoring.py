"""Pure battle scoring: rubric ballots, match decisions with tie policies, standings, timers."""
from datetime import datetime, timezone


def turn_order(round_no):
    """Odd rounds: side a opens; even rounds: side b opens (matches RAP-04 Match)."""
    return ("a", "b") if round_no % 2 else ("b", "a")


def judge_ballots(scores, criteria, rounds):
    """scores: [{judge_user_id, round, side, criterion_id, value}] → {judge: 'a'|'b'|'tie'} for the given rounds.

    Each judge's side total is the sum over counted rounds of the weighted rubric percentage.
    """
    crit = {int(c["id"]): c for c in criteria}
    totals = {}
    for s in scores:
        if int(s["round"]) not in rounds:
            continue
        c = crit.get(int(s["criterion_id"]))
        if not c:
            continue
        pct = float(s["value"]) / c["max_score"] * c["weight_bps"] / 100
        judge = totals.setdefault(s["judge_user_id"], {"a": 0.0, "b": 0.0})
        judge[s["side"]] += pct
    out = {}
    for judge, t in totals.items():
        diff = round(t["a"] - t["b"], 6)
        out[judge] = "a" if diff > 0 else "b" if diff < 0 else "tie"
    return out, totals


def audience_ballot(votes):
    a = sum(1 for v in votes if v["side"] == "a")
    b = sum(1 for v in votes if v["side"] == "b")
    if a == b:
        return None, a, b
    return ("a" if a > b else "b"), a, b


def decide(ballots, *, tie_policy, overtimes, max_overtimes, audience=None):
    """Returns (outcome, winner_side) where outcome is decided|overtime|draw|recount."""
    cast = list(ballots) + ([audience] if audience else [])
    a, b = cast.count("a"), cast.count("b")
    if a != b:
        return "decided", "a" if a > b else "b"
    if tie_policy == "overtime-round" and overtimes < max_overtimes:
        return "overtime", None
    if tie_policy == "judges-recount":
        return "recount", None
    return "draw", None


def standings(results, *, win=3, draw=1, loss=0):
    """results: [{a, b, winner (artist id|None), status}] keyed by artist ids → sorted table rows."""
    table = {}

    def row(aid, name):
        return table.setdefault(aid, {"artist_id": aid, "name": name, "played": 0, "wins": 0, "draws": 0, "losses": 0, "points": 0})

    for r in results:
        if r["status"] not in ("decided", "draw", "forfeited") or r["a"] is None or r["b"] is None:
            continue
        ra, rb = row(r["a"], r["a_name"]), row(r["b"], r["b_name"])
        ra["played"] += 1
        rb["played"] += 1
        if r["status"] == "draw" or r["winner"] is None:
            for x in (ra, rb):
                x["draws"] += 1
                x["points"] += draw
        else:
            w, l = (ra, rb) if r["winner"] == r["a"] else (rb, ra)
            w["wins"] += 1
            w["points"] += win
            l["losses"] += 1
            l["points"] += loss
    rows = sorted(table.values(), key=lambda x: (-x["points"], -x["wins"], x["losses"], x["name"].lower()))
    for i, r in enumerate(rows):
        r["rank"] = i + 1
    return rows


def timer_state(match, limit_seconds, now=None):
    now = now or datetime.now(timezone.utc)
    elapsed = int(match.get("timer_elapsed_ms") or 0)
    if match.get("timer_running") and match.get("timer_started_at"):
        elapsed += int((now - match["timer_started_at"]).total_seconds() * 1000)
    remaining = limit_seconds * 1000 - elapsed
    return {"elapsed_ms": elapsed, "remaining_ms": remaining, "running": bool(match.get("timer_running")), "over_ms": max(0, -remaining)}


def bracket_size(n):
    size = 2
    while size < n:
        size *= 2
    return size


def split(amount_cents, rule):
    """Split cents by basis points; rounding remainder goes to the league so totals always reconcile."""
    parts = {k: amount_cents * rule[k + "_bps"] // 10000 for k in ("platform", "league", "artist", "event_pool")}
    parts["league"] += amount_cents - sum(parts.values())
    return parts
