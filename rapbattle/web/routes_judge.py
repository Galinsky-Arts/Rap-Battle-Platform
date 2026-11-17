"""Judges score each side per round against the event rubric."""
import re

from .http import HttpError, integer
from .views import M, csrf_field, table, h, join, fmt_ms
from ..engine import scoring


def mount(r, app):
    eng = app.engine

    @r.get("/judge")
    def judge_home(ctx):
        u = ctx.require()
        events = eng.judge_events(u.id)
        body = M("<h1>Judging</h1>")
        for e in events:
            ms = [m for m in eng.matches(e["id"]) if m["status"] in ("in_progress", "judging", "overtime")]
            body += M(f"<section class='card'><h2>{h(e['title'])} <span class='pill'>{h(e['status'])}</span></h2>") + table(["Battle", "Status", ""], [[f"{m['a_name']} vs {m['b_name']}", m["status"], M(f"<a href='/judge/battles/{m['id']}'>Score</a>")] for m in ms], "No battle is being judged right now.") + M("</section>")
        if not events:
            body += M("<p class='muted'>You are not assigned to any event.</p>")
        return ctx.page("Judging", body)

    @r.get("/judge/battles/{mid}")
    def judge_battle(ctx, mid):
        u = ctx.require()
        m = eng.match(integer(mid, lo=1))
        if not eng.is_judge(m["event_id"], u.id):
            raise HttpError(403, "You are not a judge for this event")
        crit = eng.criteria(m["event_id"])
        mine = {(s["round"], s["side"], s["criterion_id"]): s["value"] for s in eng.scores_for(m["id"], u.id)}
        rounds = list(range(1, max(m["current_round"], 1) + 1))
        open_ = m["status"] in ("in_progress", "judging", "overtime")
        cfg = m["config"]
        body = M(f"<p class='eyebrow'>{h(m['event_title'])}</p><h1>{h(m['a_name'])} vs {h(m['b_name'])}</h1><p>Status {h(m['status'])}{' · recount required: re-enter all scores' if m['outcome_reason'] == 'recount-required' else ''}</p>")
        if m["status"] in ("in_progress", "overtime"):
            limit = cfg["overtime_seconds"] if m["current_round"] > cfg["rounds"] else cfg["round_seconds"]
            t = scoring.timer_state(m, limit)
            body += M(f"<p class='timer' data-remaining-ms='{t['remaining_ms']}' data-running='{1 if t['running'] else 0}'>{fmt_ms(t['remaining_ms'])}</p>")
        body += M(f"<form method='post' action='/judge/battles/{m['id']}'>") + csrf_field(ctx.csrf)
        for rnd in rounds:
            label = f"Round {rnd}" if rnd <= cfg["rounds"] else f"Overtime {rnd - cfg['rounds']}"
            body += M(f"<section class='card'><h2>{label}</h2><div class='grid'>")
            for side, name in (("a", m["a_name"]), ("b", m["b_name"])):
                body += M(f"<div><h3>{h(name)}</h3>") + join(M(f"<label class='field'><span>{h(c['name'])} (0–{c['max_score']}, {c['weight_bps'] / 100:g}%)</span><input type='number' step='0.5' min='0' max='{c['max_score']}' name='s_{rnd}_{side}_{c['id']}' value='{h(mine.get((rnd, side, c['id']), ''))}'{'' if open_ else ' disabled'}></label>") for c in crit) + M("</div>")
            body += M("</div></section>")
        body += (M("<button type='submit'>Save scores</button>") if open_ else M("")) + M("</form>")
        return ctx.page("Score battle", body, refresh=None)

    @r.post("/judge/battles/{mid}")
    def save(ctx, mid):
        u = ctx.require()
        values = {}
        for k, v in ctx.form.items():
            m = re.fullmatch(r"s_(\d{1,2})_([ab])_(\d{1,12})", k)
            if m and str(v).strip() != "":
                values[(int(m.group(1)), m.group(2), int(m.group(3)))] = v
        eng.submit_scores(u.id, integer(mid, lo=1), values)
        return ctx.redirect(f"/judge/battles/{mid}", "Scores saved.")
