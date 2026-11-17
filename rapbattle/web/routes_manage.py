"""RAP-07 league administration: settings, staff, seasons, events, lineups, brackets, judges, live battle
console, moderation, contracts, disputes/corrections (RAP-09), sponsorship (RAP-11) and revenue (RAP-10)."""
import re
from datetime import datetime, timezone

from .http import HttpError, text, integer, checkbox, one_of, money
from .views import M, field, textarea, select, check, csrf_field, table, post_button, h, join, fmt_dt, fmt_ms, embed
from .media import media_link
from .security import norm_email
from ..web.db import audit, notify
from ..engine import scoring
from .. import presets as preset_rules
from .. import payments

EVENT_STATUS = [("draft", "Draft"), ("scheduled", "Scheduled"), ("live", "Live"), ("completed", "Completed"), ("cancelled", "Cancelled")]


def dt(v):
    s = str(v or "").strip()
    if not s:
        return None
    try:
        d = datetime.fromisoformat(s)
    except ValueError:
        raise HttpError(400, "Invalid date")
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def mount(r, app):
    db, eng = app.db, app.engine

    def guard(ctx, slug):
        ctx.require_league(slug, "producer")
        return app.league(slug)

    def nav(slug):
        items = [("", "Dashboard"), ("/events", "Events"), ("/artists", "Emcees & contracts"), ("/disputes", "Disputes"), ("/sponsors", "Sponsorship"), ("/revenue", "Revenue")]
        return M("<nav class='row card' aria-label='League admin'>") + join(M(f"<a href='/manage/{h(slug)}{p}'>{l}</a>") for p, l in items) + M("</nav>")

    def page(ctx, slug, title, body):
        return ctx.page(title, nav(slug) + body, league=slug)

    @r.get("/manage")
    def manage_home(ctx):
        u = ctx.require()
        if u.is_admin:
            leagues = db.all("SELECT slug, name FROM leagues ORDER BY name")
        else:
            leagues = db.all("SELECT l.slug, l.name FROM leagues l JOIN league_members m ON m.league_slug=l.slug WHERE m.user_id=%s AND m.role='producer' ORDER BY l.name", (u.id,))
        if not leagues:
            raise HttpError(403, "You do not manage any league")
        return ctx.page("Manage", M("<h1>Your leagues</h1><ul class='grid'>") + join(M(f"<li class='card'><a href='/manage/{h(l['slug'])}'>{h(l['name'])}</a></li>") for l in leagues) + M("</ul>"))

    @r.get("/manage/{slug}")
    def dashboard(ctx, slug):
        lg = guard(ctx, slug)
        st = db.one("SELECT * FROM league_settings WHERE league_slug=%s", (slug,)) or {"city": "", "region": "", "description": "", "listed": True, "sponsorship_enabled": False, "contact_email": ""}
        staff = db.all("SELECT m.*, u.display_name, u.email FROM league_members m JOIN users u ON u.id=m.user_id WHERE m.league_slug=%s ORDER BY m.role, u.display_name", (slug,))
        seasons = db.all("SELECT * FROM seasons WHERE league_slug=%s ORDER BY id DESC", (slug,))
        pending = db.one("SELECT (SELECT count(*) FROM lineup_requests lr JOIN events e ON e.id=lr.event_id WHERE e.league_slug=%s AND lr.status='pending') AS lineup, (SELECT count(*) FROM disputes d JOIN matches m ON m.id=d.match_id JOIN events e ON e.id=m.event_id WHERE e.league_slug=%s AND d.status='open') AS disputes", (slug, slug))
        body = M(f"<h1>{h(lg['name'])}</h1><p>{pending['lineup']} pending lineup request(s) · {pending['disputes']} open dispute(s) · <a href='/l/{h(slug)}'>public page</a> · branding via <code>PUT /api/leagues/{h(slug)}/branding</code></p>")
        body += (M(f"<form method='post' action='/manage/{h(slug)}/settings' class='card'><h2>League profile and listing</h2>") + csrf_field(ctx.csrf) + field("City", "city", value=st["city"]) + field("Region / state", "region", value=st["region"])
                 + textarea("Description", "description", value=st["description"]) + field("Public contact email", "contact_email", value=st["contact_email"]) + check("Listed in league discovery", "listed", st["listed"])
                 + check("Sponsorship placements enabled for this league (opt-in)", "sponsorship_enabled", st["sponsorship_enabled"]) + M("<button type='submit'>Save</button></form>"))
        body += M("<section class='card'><h2>Staff</h2>") + table(["Name", "Email", "Role", ""], [[s["display_name"], s["email"], s["role"], post_button(f"/manage/{slug}/staff/remove", "Remove", ctx.csrf, fields={"user_id": s["user_id"], "role": s["role"]}, cls="secondary")] for s in staff], "No staff yet.")
        body += (M(f"<form method='post' action='/manage/{h(slug)}/staff' class='row'>") + csrf_field(ctx.csrf) + field("Account email", "email", required=True) + select("Role", "role", [("judge", "Judge"), ("producer", "Promoter / producer")]) + M("<button type='submit'>Add</button></form></section>"))
        body += M("<section class='card'><h2>Seasons</h2>") + table(["Season", "Status", "Points W/D/L", ""], [[s["name"], s["status"], f"{s['points_win']}/{s['points_draw']}/{s['points_loss']}", post_button(f"/manage/{slug}/seasons/{s['id']}/close", "Close season", ctx.csrf, cls="secondary") if s["status"] == "active" else ""] for s in seasons], "No seasons yet.")
        body += (M(f"<form method='post' action='/manage/{h(slug)}/seasons' class='row'>") + csrf_field(ctx.csrf) + field("Name", "name", required=True) + field("Win pts", "win", value="3") + field("Draw pts", "draw", value="1") + field("Loss pts", "loss", value="0") + M("<button type='submit'>Add season</button></form></section>"))
        return page(ctx, slug, "Manage " + lg["name"], body)

    @r.post("/manage/{slug}/settings")
    def save_settings(ctx, slug):
        guard(ctx, slug)
        f = ctx.form
        db.run("""INSERT INTO league_settings(league_slug,city,region,description,contact_email,listed,sponsorship_enabled) VALUES (%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (league_slug) DO UPDATE SET city=EXCLUDED.city, region=EXCLUDED.region, description=EXCLUDED.description, contact_email=EXCLUDED.contact_email,
            listed=EXCLUDED.listed, sponsorship_enabled=EXCLUDED.sponsorship_enabled""",
               (slug, text(f.get("city"), max_len=80), text(f.get("region"), max_len=80), text(f.get("description"), max_len=2000), text(f.get("contact_email"), max_len=200),
                checkbox(f.get("listed")), checkbox(f.get("sponsorship_enabled"))))
        audit(db, ctx.user.id, "league.settings", {}, slug)
        return ctx.redirect(f"/manage/{slug}", "League settings saved.")

    @r.post("/manage/{slug}/staff")
    def add_staff(ctx, slug):
        guard(ctx, slug)
        u = db.one("SELECT id FROM users WHERE email=%s", (norm_email(ctx.form.get("email")),))
        if not u:
            raise HttpError(400, "No account with that email; ask them to register first")
        role = one_of(ctx.form.get("role"), ("judge", "producer"), "role")
        db.run("INSERT INTO league_members(league_slug,user_id,role) VALUES (%s,%s,%s) ON CONFLICT DO NOTHING", (slug, u["id"], role))
        notify(db, u["id"], f"You were added as {role} in {slug}.")
        audit(db, ctx.user.id, "staff.added", {"userId": u["id"], "role": role}, slug)
        return ctx.redirect(f"/manage/{slug}", "Staff member added.")

    @r.post("/manage/{slug}/staff/remove")
    def remove_staff(ctx, slug):
        guard(ctx, slug)
        uid = integer(ctx.form.get("user_id"), lo=1)
        role = one_of(ctx.form.get("role"), ("judge", "producer"), "role")
        if uid == ctx.user.id and role == "producer" and not ctx.user.is_admin:
            raise HttpError(400, "You cannot remove your own producer role")
        db.run("DELETE FROM league_members WHERE league_slug=%s AND user_id=%s AND role=%s", (slug, uid, role))
        audit(db, ctx.user.id, "staff.removed", {"userId": uid, "role": role}, slug)
        return ctx.redirect(f"/manage/{slug}", "Staff member removed.")

    @r.post("/manage/{slug}/seasons")
    def add_season(ctx, slug):
        guard(ctx, slug)
        db.run("INSERT INTO seasons(league_slug,name,points_win,points_draw,points_loss) VALUES (%s,%s,%s,%s,%s)",
               (slug, text(ctx.form.get("name"), min_len=1, max_len=80, name="Name"), integer(ctx.form.get("win"), lo=0, hi=10), integer(ctx.form.get("draw"), lo=0, hi=10), integer(ctx.form.get("loss"), lo=0, hi=10)))
        return ctx.redirect(f"/manage/{slug}", "Season added.")

    @r.post("/manage/{slug}/seasons/{sid}/close")
    def close_season(ctx, slug, sid):
        guard(ctx, slug)
        db.run("UPDATE seasons SET status='closed' WHERE id=%s AND league_slug=%s", (integer(sid, lo=1), slug))
        return ctx.redirect(f"/manage/{slug}", "Season closed.")

    # ---------- events ----------
    @r.get("/manage/{slug}/events")
    def events(ctx, slug):
        guard(ctx, slug)
        evs = eng.list_events(slug)
        seasons = db.all("SELECT id, name FROM seasons WHERE league_slug=%s AND status='active' ORDER BY id DESC", (slug,))
        presets = {p["id"]: p for p in app.store.presets(slug)}
        body = M("<h1>Events</h1>") + table(["Event", "Status", "Published", "Starts", "Format"], [[M(f"<a href='/manage/{h(slug)}/events/{e['id']}'>{h(e['title'])}</a>"), e["status"], "Yes" if e["published"] else "No", fmt_dt(e["starts_at"]), e["preset_id"]] for e in evs], "No events yet.")
        body += (M(f"<form method='post' action='/manage/{h(slug)}/events' class='card'><h2>New event</h2>") + csrf_field(ctx.csrf) + field("Title", "title", required=True) + field("Slug", "event_slug", required=True, hint="Lowercase letters, digits and hyphens.")
                 + select("Format preset", "preset", [(k, preset_rules.PRESETS[k]["label"]) for k in presets]) + select("Season", "season_id", [("", "No season")] + [(s["id"], s["name"]) for s in seasons])
                 + field("Starts at (UTC)", "starts_at", type_="datetime-local") + field("Venue", "venue") + textarea("Description", "description") + M("<button type='submit'>Create event</button></form>"))
        return page(ctx, slug, "Events", body)

    @r.post("/manage/{slug}/events")
    def create_event(ctx, slug):
        guard(ctx, slug)
        f = ctx.form
        pid = one_of(f.get("preset"), preset_rules.PRESET_IDS, "preset")
        cfg = next(p["config"] for p in app.store.presets(slug) if p["id"] == pid)
        es = text(f.get("event_slug"), min_len=2, max_len=64, name="Slug")
        if not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?", es):
            raise HttpError(400, "Slug must be lowercase letters, digits and hyphens")
        sid = integer(f.get("season_id"), lo=1, default=0) or None
        if sid and not db.one("SELECT 1 FROM seasons WHERE id=%s AND league_slug=%s", (sid, slug)):
            raise HttpError(400, "Unknown season")
        e = eng.create_event(slug, slug=es, title=text(f.get("title"), min_len=2, max_len=160, name="Title"), preset_id=pid, config=dict(cfg), season_id=sid,
                             starts_at=dt(f.get("starts_at")), venue=text(f.get("venue"), max_len=160), description=text(f.get("description"), max_len=4000))
        audit(db, ctx.user.id, "event.created", {"eventId": e["id"]}, slug)
        return ctx.redirect(f"/manage/{slug}/events/{e['id']}", "Event created.")

    @r.get("/manage/{slug}/events/{eid}")
    def event_detail(ctx, slug, eid):
        guard(ctx, slug)
        e = eng.event(integer(eid, lo=1), slug)
        crit, ents, judges, ms = eng.criteria(e["id"]), eng.entrants(e["id"]), eng.judges(e["id"]), eng.matches(e["id"])
        reqs = db.all("SELECT lr.*, a.stage_name FROM lineup_requests lr JOIN artists a ON a.id=lr.artist_id WHERE lr.event_id=%s ORDER BY lr.id", (e["id"],))
        roster = db.all("SELECT id, stage_name FROM artists WHERE league_slug=%s AND status='active' AND consent_profile ORDER BY stage_name", (slug,))
        seasons = db.all("SELECT id, name FROM seasons WHERE league_slug=%s ORDER BY id DESC", (slug,))
        judge_pool = db.all("SELECT u.id, u.display_name FROM league_members m JOIN users u ON u.id=m.user_id WHERE m.league_slug=%s AND m.role='judge' ORDER BY u.display_name", (slug,))
        cfg = e["config"]
        active = [x for x in ents if x["status"] == "active"]
        body = M(f"<p class='eyebrow'>{h(e['preset_id'])} · {cfg['rounds']} × {cfg['round_seconds']}s · {h(cfg['style'])} · tie: {h(cfg['tie_policy'])} · <a href='/l/{h(slug)}/events/{h(e['slug'])}'>public page</a></p><h1>{h(e['title'])}</h1>")
        body += (M(f"<form method='post' action='/manage/{h(slug)}/events/{e['id']}' class='card'><h2>Details</h2>") + csrf_field(ctx.csrf) + field("Title", "title", value=e["title"], required=True)
                 + field("Starts at (UTC)", "starts_at", type_="datetime-local", value=e["starts_at"].strftime("%Y-%m-%dT%H:%M") if e["starts_at"] else "") + field("Venue", "venue", value=e["venue"])
                 + textarea("Description", "description", value=e["description"]) + select("Status", "status", EVENT_STATUS, e["status"]) + check("Published", "published", e["published"])
                 + select("Season", "season_id", [("", "No season")] + [(s["id"], s["name"]) for s in seasons], e["season_id"] or "")
                 + field("Stream link", "stream_url", type_="url", value=e["stream_url"] or "", hint="YouTube, Vimeo or SoundCloud.") + check("Audience voting enabled", "voting_enabled", e["voting_enabled"])
                 + field("Ticket price (USD, 0 = free)", "ticket", value=f"{e['ticket_cents'] / 100:.2f}") + field("Pay-per-view price (USD, 0 = free stream)", "ppv", value=f"{e['ppv_cents'] / 100:.2f}")
                 + M(f"<p class='muted'>Crowd vote counts as one ballot only if the format enables it (currently {'on' if cfg.get('audience_vote_enabled') else 'off'}; change in the league's presets).{'' if app.stripe.enabled else ' Payments are off platform-wide, so prices are shown but not sold.'}</p><button type='submit'>Save</button></form>"))
        body += M("<section class='card'><h2>Judging rubric</h2>") + table(["Criterion", "Weight", "Max", ""], [[c["name"], f"{c['weight_bps'] / 100:g}%", c["max_score"], post_button(f"/manage/{slug}/events/{e['id']}/criteria/{c['id']}/delete", "Remove", ctx.csrf, cls="secondary")] for c in crit])
        body += M(f"<form method='post' action='/manage/{h(slug)}/events/{e['id']}/criteria' class='row'>") + csrf_field(ctx.csrf) + field("Name", "name", required=True) + field("Weight %", "weight", required=True) + field("Max", "max_score", value="10") + M("<button type='submit'>Add</button></form></section>")
        body += M("<section class='card'><h2>Lineup requests</h2>") + table(["Emcee", "Note", "Status", ""], [[x["stage_name"], x["note"], x["status"], (post_button(f"/manage/{slug}/lineup/{x['id']}", "Accept", ctx.csrf, fields={"decision": "accepted"}) + post_button(f"/manage/{slug}/lineup/{x['id']}", "Decline", ctx.csrf, fields={"decision": "declined"}, cls="secondary")) if x["status"] == "pending" else ""] for x in reqs], "No requests.") + M("</section>")
        body += M("<section class='card'><h2>Lineup</h2>") + table(["Seed", "Emcee", "Status", ""], [[x["seed"] or "", x["display_name"], x["status"], M(f"<form method='post' action='/manage/{h(slug)}/entrants/{x['id']}' class='row'>") + csrf_field(ctx.csrf) + M(f"<input name='seed' value='{h(x['seed'] or '')}' size='3' aria-label='Seed'><select name='status' aria-label='Status'>") + join(M(f"<option{' selected' if s == x['status'] else ''}>{s}</option>") for s in ("active", "withdrawn", "disqualified")) + M("</select><button class='secondary' type='submit'>Set</button></form>")] for x in ents], "Nobody on the lineup yet.")
        body += (M(f"<form method='post' action='/manage/{h(slug)}/events/{e['id']}/entrants' class='row'>") + csrf_field(ctx.csrf) + select("Add from roster", "artist_id", [("", "—")] + [(a["id"], a["stage_name"]) for a in roster]) + field("…or guest stage name", "display_name") + M("<button type='submit'>Add to lineup</button></form>"))
        body += post_button(f"/manage/{slug}/events/{e['id']}/seed", "Seed lineup from standings", ctx.csrf, cls="secondary") + M("</section>")
        body += M("<section class='card'><h2>Judges</h2>") + table(["Judge", ""], [[j["display_name"], post_button(f"/manage/{slug}/events/{e['id']}/judges/remove", "Remove", ctx.csrf, fields={"user_id": j["id"]}, cls="secondary")] for j in judges], f"No judges assigned (format expects {cfg['judges']}).")
        body += M(f"<form method='post' action='/manage/{h(slug)}/events/{e['id']}/judges' class='row'>") + csrf_field(ctx.csrf) + select("League judge", "user_id", [(j["id"], j["display_name"]) for j in judge_pool]) + M("<button type='submit'>Assign</button></form></section>")
        body += M("<section class='card'><h2>Battles</h2>") + table(["Stage", "Battle", "Status", ""], [[m["stage"], f"{m['a_name'] or 'TBD'} vs {m['b_name'] or 'TBD'}", m["status"], M(f"<a href='/manage/{h(slug)}/battles/{m['id']}'>Run</a>")] for m in ms], "No battles yet.")
        if not ms:
            body += post_button(f"/manage/{slug}/events/{e['id']}/bracket", "Generate seeded bracket", ctx.csrf)
        if not any(m["stage"] > 1 for m in ms):
            opts = [(x["id"], x["display_name"]) for x in active]
            body += M(f"<form method='post' action='/manage/{h(slug)}/events/{e['id']}/matches' class='row'>") + csrf_field(ctx.csrf) + select("Side A", "side_a", opts) + select("Side B", "side_b", opts) + M("<button type='submit' class='secondary'>Add single battle</button></form>")
        body += M("</section><section class='card'><h2>Finalize</h2><p>Records the champion (final of a bracket, or the winner of a one-battle card) and completes the event.</p>") + post_button(f"/manage/{slug}/events/{e['id']}/finalize", "Finalize event", ctx.csrf, confirm="Finalize this event?") + M("</section>")
        return page(ctx, slug, e["title"], body)

    def ev(ctx, slug, eid):
        guard(ctx, slug)
        return eng.event(integer(eid, lo=1), slug)

    @r.post("/manage/{slug}/events/{eid}")
    def update_event(ctx, slug, eid):
        e = ev(ctx, slug, eid)
        f = ctx.form
        sid = integer(f.get("season_id"), lo=1, default=0) or None
        if sid and not db.one("SELECT 1 FROM seasons WHERE id=%s AND league_slug=%s", (sid, slug)):
            raise HttpError(400, "Unknown season")
        eng.update_event(e["id"], title=text(f.get("title"), min_len=2, max_len=160, name="Title"), starts_at=dt(f.get("starts_at")), venue=text(f.get("venue"), max_len=160),
                         description=text(f.get("description"), max_len=4000), status=one_of(f.get("status"), [x[0] for x in EVENT_STATUS], "status"), published=checkbox(f.get("published")),
                         season_id=sid, stream_url=media_link(f.get("stream_url"), name="Stream link"), voting_enabled=checkbox(f.get("voting_enabled")),
                         ticket_cents=money(f.get("ticket"), name="Ticket price"), ppv_cents=money(f.get("ppv"), name="PPV price"))
        audit(db, ctx.user.id, "event.updated", {"eventId": e["id"]}, slug)
        return ctx.redirect(f"/manage/{slug}/events/{e['id']}", "Event saved.")

    @r.post("/manage/{slug}/events/{eid}/criteria")
    def add_criterion(ctx, slug, eid):
        e = ev(ctx, slug, eid)
        try:
            w = round(float(ctx.form.get("weight")) * 100)
        except (TypeError, ValueError):
            raise HttpError(400, "Weight must be a number")
        if not 1 <= w <= 10000:
            raise HttpError(400, "Weight must be between 0.01 and 100")
        eng.add_criterion(e["id"], text(ctx.form.get("name"), min_len=1, max_len=60, name="Name"), w, integer(ctx.form.get("max_score"), lo=1, hi=100))
        return ctx.redirect(f"/manage/{slug}/events/{e['id']}", "Criterion added.")

    @r.post("/manage/{slug}/events/{eid}/criteria/{cid}/delete")
    def del_criterion(ctx, slug, eid, cid):
        e = ev(ctx, slug, eid)
        eng.remove_criterion(e["id"], integer(cid, lo=1))
        return ctx.redirect(f"/manage/{slug}/events/{e['id']}", "Criterion removed.")

    @r.post("/manage/{slug}/lineup/{rid}")
    def lineup_decision(ctx, slug, rid):
        guard(ctx, slug)
        req = db.one("SELECT lr.*, a.stage_name, a.user_id FROM lineup_requests lr JOIN artists a ON a.id=lr.artist_id JOIN events e ON e.id=lr.event_id WHERE lr.id=%s AND e.league_slug=%s", (integer(rid, lo=1), slug))
        if not req or req["status"] != "pending":
            raise HttpError(404, "Request not found")
        decision = one_of(ctx.form.get("decision"), ("accepted", "declined"), "decision")
        if decision == "accepted":
            eng.add_entrant(req["event_id"], req["stage_name"], artist_id=req["artist_id"])
        db.run("UPDATE lineup_requests SET status=%s WHERE id=%s", (decision, req["id"]))
        if req["user_id"]:
            notify(db, req["user_id"], f"Your lineup request was {decision}.")
        return ctx.redirect(f"/manage/{slug}/events/{req['event_id']}", "Lineup request " + decision + ".")

    @r.post("/manage/{slug}/events/{eid}/entrants")
    def add_entrant(ctx, slug, eid):
        e = ev(ctx, slug, eid)
        aid = integer(ctx.form.get("artist_id"), lo=1, default=0)
        if aid:
            a = db.one("SELECT * FROM artists WHERE id=%s AND league_slug=%s AND status='active'", (aid, slug))
            if not a:
                raise HttpError(400, "Unknown emcee")
            eng.add_entrant(e["id"], a["stage_name"], artist_id=a["id"])
        else:
            eng.add_entrant(e["id"], text(ctx.form.get("display_name"), min_len=1, max_len=60, name="Stage name"))
        return ctx.redirect(f"/manage/{slug}/events/{e['id']}", "Added to the lineup.")

    @r.post("/manage/{slug}/entrants/{nid}")
    def set_entrant(ctx, slug, nid):
        guard(ctx, slug)
        en = db.one("SELECT en.* FROM entrants en JOIN events e ON e.id=en.event_id WHERE en.id=%s AND e.league_slug=%s", (integer(nid, lo=1), slug))
        if not en:
            raise HttpError(404, "Not found")
        seed = integer(ctx.form.get("seed"), lo=1, hi=999, default=0) or None
        eng.set_entrant(en["id"], status=ctx.form.get("status"), seed=seed)
        return ctx.redirect(f"/manage/{slug}/events/{en['event_id']}", "Lineup updated.")

    @r.post("/manage/{slug}/events/{eid}/seed")
    def seed_from_standings(ctx, slug, eid):
        e = ev(ctx, slug, eid)
        order = {row["artist_id"]: row["rank"] for row in eng.standings(slug, e["season_id"])}
        ents = [x for x in eng.entrants(e["id"]) if x["status"] == "active"]
        ranked = sorted(ents, key=lambda x: (order.get(x["artist_id"], 10_000), x["id"]))
        for i, x in enumerate(ranked):
            eng.set_entrant(x["id"], seed=i + 1)
        return ctx.redirect(f"/manage/{slug}/events/{e['id']}", "Seeds set from standings.")

    @r.post("/manage/{slug}/events/{eid}/judges")
    def assign_judge(ctx, slug, eid):
        e = ev(ctx, slug, eid)
        uid = integer(ctx.form.get("user_id"), lo=1)
        eng.assign_judge(e["id"], uid)
        notify(db, uid, f"You were assigned to judge {e['title']}.")
        return ctx.redirect(f"/manage/{slug}/events/{e['id']}", "Judge assigned.")

    @r.post("/manage/{slug}/events/{eid}/judges/remove")
    def remove_judge(ctx, slug, eid):
        e = ev(ctx, slug, eid)
        eng.remove_judge(e["id"], integer(ctx.form.get("user_id"), lo=1))
        return ctx.redirect(f"/manage/{slug}/events/{e['id']}", "Judge removed.")

    @r.post("/manage/{slug}/events/{eid}/bracket")
    def bracket(ctx, slug, eid):
        e = ev(ctx, slug, eid)
        stages = eng.generate_bracket(e["id"])
        audit(db, ctx.user.id, "bracket.generated", {"eventId": e["id"], "stages": stages}, slug)
        return ctx.redirect(f"/manage/{slug}/events/{e['id']}", f"Bracket generated with {stages} stage(s).")

    @r.post("/manage/{slug}/events/{eid}/matches")
    def add_match(ctx, slug, eid):
        e = ev(ctx, slug, eid)
        eng.add_match(e["id"], integer(ctx.form.get("side_a"), lo=1), integer(ctx.form.get("side_b"), lo=1))
        return ctx.redirect(f"/manage/{slug}/events/{e['id']}", "Battle added.")

    @r.post("/manage/{slug}/events/{eid}/finalize")
    def finalize(ctx, slug, eid):
        e = ev(ctx, slug, eid)
        out = eng.finalize_event(e["id"])
        audit(db, ctx.user.id, "event.finalized", {"eventId": e["id"], "champion": out["champion_entrant_id"]}, slug)
        return ctx.redirect(f"/manage/{slug}/events/{e['id']}", "Event finalized." + (" Champion recorded." if out["champion_entrant_id"] else ""))

    # ---------- live battle console ----------
    @r.get("/manage/{slug}/battles/{mid}")
    def console(ctx, slug, mid):
        guard(ctx, slug)
        m = eng.match(integer(mid, lo=1), slug)
        cfg = m["config"]
        judges = eng.judges(m["event_id"])
        scored = {r_["judge_user_id"]: r_["n"] for r_ in db.all("SELECT judge_user_id, count(*)::int AS n FROM scores WHERE match_id=%s GROUP BY 1", (m["id"],))}
        vc = eng.vote_counts(m["id"])
        turns = eng.turns(m["id"])
        u = f"/manage/{slug}/battles/{m['id']}"
        body = M(f"<p class='eyebrow'><a href='/manage/{h(slug)}/events/{m['event_id']}'>{h(m['event_title'])}</a> · stage {m['stage']}</p><h1>{h(m['a_name'] or 'TBD')} vs {h(m['b_name'] or 'TBD')}</h1>"
                 f"<p><span class='pill'>{h(m['status'])}</span> round {m['current_round']} of {cfg['rounds']}{' + OT ' + str(m['overtimes']) if m['overtimes'] else ''} · {h(m['outcome_reason'])}</p>")
        if m["status"] == "scheduled":
            body += M("<div class='row'>") + post_button(u + "/start", "Start battle", ctx.csrf) + post_button(u + "/cancel", "Cancel battle", ctx.csrf, cls="secondary") + M("</div>")
        if m["status"] in ("in_progress", "overtime"):
            limit = cfg["overtime_seconds"] if m["current_round"] > cfg["rounds"] else cfg["round_seconds"]
            t = scoring.timer_state(m, limit)
            nxt = eng.expected_side(m)
            who = m["a_name"] if (m["timer_side"] or nxt) == "a" else m["b_name"]
            body += M(f"<section class='card'><h2>{'On the mic' if m['timer_side'] else 'Up next'}: {h(who)}</h2><p class='timer{' over' if t['remaining_ms'] < 0 else ''}' data-remaining-ms='{t['remaining_ms']}' data-running='{1 if t['running'] else 0}'>{fmt_ms(t['remaining_ms'])}</p><div class='row'>")
            if not m["timer_side"]:
                body += post_button(u + "/timer", "Start turn", ctx.csrf, fields={"action": "start"})
            else:
                body += (post_button(u + "/timer", "Pause", ctx.csrf, fields={"action": "pause"}, cls="secondary") if t["running"] else post_button(u + "/timer", "Resume", ctx.csrf, fields={"action": "resume"}, cls="secondary")) + post_button(u + "/timer", "End turn", ctx.csrf, fields={"action": "stop"})
            body += M("</div></section>")
        body += M("<section class='card'><h2>Turns</h2>") + table(["Round", "Side", "Time", "Over"], [[t_["round"], m["a_name"] if t_["side"] == "a" else m["b_name"], fmt_ms(t_["elapsed_ms"]), fmt_ms(t_["over_ms"]) if t_["over_ms"] else ""] for t_ in turns], "No turns yet.") + M("</section>")
        body += M("<section class='card'><h2>Judges and crowd</h2>") + table(["Judge", "Scores entered"], [[j["display_name"], scored.get(j["id"], 0)] for j in judges], "No judges assigned.")
        body += M(f"<p>Crowd votes: {h(m['a_name'])} {vc['a']} · {h(m['b_name'])} {vc['b']}{' (counts as one ballot)' if cfg.get('audience_vote_enabled') else ' (shown only)'}</p>")
        if m["voting_enabled"] and m["status"] in ("in_progress", "judging", "overtime"):
            body += post_button(u + "/voting", "Close crowd vote" if m["voting_open"] else "Open crowd vote", ctx.csrf, fields={"open": "0" if m["voting_open"] else "1"}, cls="secondary")
        body += M("</section>")
        if m["status"] == "judging":
            body += M("<section class='card'><h2>Decision</h2><p>Tallies one ballot per judge from their rubric totals, applies the format's tie rule.</p>") + post_button(u + "/close", "Close judging and decide", ctx.csrf) + M("</section>")
        if m["status"] in ("scheduled", "in_progress", "judging", "overtime") and m["side_a"] and m["side_b"]:
            body += (M(f"<form method='post' action='{u}/forfeit' class='card row'>") + csrf_field(ctx.csrf) + select("Side that forfeits", "side", [("a", m["a_name"]), ("b", m["b_name"])])
                     + select("Reason", "reason", [("forfeit", "Forfeit"), ("no-show", "No-show"), ("disqualified", "Disqualified")]) + M("<button type='submit' class='danger'>Record forfeit</button></form>"))
        if m["status"] in ("decided", "draw", "forfeited"):
            body += (M(f"<form method='post' action='{u}/correct' class='card row'><h2>Correction</h2>") + csrf_field(ctx.csrf) + select("Corrected result", "winner", [("a", m["a_name"] + " wins"), ("b", m["b_name"] + " wins"), ("draw", "Draw")])
                     + M("<button type='submit' class='secondary'>Apply correction</button></form>"))
            body += M(f"<form method='post' action='{u}/recording' class='card row'>") + csrf_field(ctx.csrf) + field("Recording link", "recording_url", type_="url", value=m["recording_url"] or "") + M("<button type='submit' class='secondary'>Save</button></form>")
            if m["recording_url"]:
                body += embed(m["recording_url"], "Recording")
        refresh = 15 if m["status"] in ("in_progress", "overtime", "judging") else None
        return ctx.page("Battle console", nav(slug) + body, league=slug, refresh=refresh)

    def battle_action(path, fn, msg):
        def handler(ctx, slug, mid):
            guard(ctx, slug)
            m = eng.match(integer(mid, lo=1), slug)
            out = fn(ctx, m)
            audit(db, ctx.user.id, "battle." + path, {"matchId": m["id"], **({"action": ctx.form.get("action")} if ctx.form.get("action") else {})}, slug)
            return ctx.redirect(f"/manage/{slug}/battles/{m['id']}", out if isinstance(out, str) else msg)
        r.add("POST", "/manage/{slug}/battles/{mid}/" + path, handler)

    battle_action("start", lambda ctx, m: eng.start_match(m["id"]), "Battle started.")
    battle_action("cancel", lambda ctx, m: eng.cancel(m["id"]), "Battle cancelled.")
    battle_action("timer", lambda ctx, m: eng.timer(m["id"], ctx.form.get("action")), "Timer updated.")
    battle_action("voting", lambda ctx, m: eng.set_voting(m["id"], ctx.form.get("open") == "1"), "Crowd vote updated.")

    def close(ctx, m):
        out = eng.close_match(m["id"])
        return {"decided": "Decision recorded.", "overtime": "Tie: overtime round added.", "recount": "Tie: judges must re-score (recount).", "draw": "Battle declared a draw."}[out["outcome"]]
    battle_action("close", close, "")
    battle_action("forfeit", lambda ctx, m: eng.forfeit(m["id"], ctx.form.get("side"), ctx.form.get("reason")), "Forfeit recorded.")
    battle_action("correct", lambda ctx, m: eng.correct(m["id"], one_of(ctx.form.get("winner"), ("a", "b", "draw"), "result")), "Result corrected.")
    battle_action("recording", lambda ctx, m: eng.set_recording(m["id"], media_link(ctx.form.get("recording_url"), name="Recording link")), "Recording saved.")

    # ---------- moderation & contracts ----------
    @r.get("/manage/{slug}/artists")
    def artists(ctx, slug):
        guard(ctx, slug)
        rows = db.all("SELECT * FROM artists WHERE league_slug=%s ORDER BY stage_name", (slug,))
        contracts = db.all("SELECT c.*, a.stage_name FROM contracts c JOIN artists a ON a.id=c.artist_id WHERE c.league_slug=%s ORDER BY c.id DESC", (slug,))
        body = M("<h1>Emcees</h1>") + table(["Emcee", "Visibility", "Status", "Payout consent", ""], [[M(f"<a href='/l/{h(slug)}/artists/{a['id']}'>{h(a['stage_name'])}</a>"), a["visibility"], a["status"] + (f" ({a['moderation_note']})" if a["moderation_note"] else ""), "Yes" if a["payout_consent"] else "No",
                                                                                                    M(f"<form method='post' action='/manage/{h(slug)}/artists/{a['id']}/moderate' class='row'>") + csrf_field(ctx.csrf) + M(f"<input name='note' placeholder='Note' aria-label='Moderation note' value='{h(a['moderation_note'])}'><input type='hidden' name='status' value='{'active' if a['status'] == 'hidden' else 'hidden'}'><button type='submit' class='secondary'>{'Unhide' if a['status'] == 'hidden' else 'Hide'}</button></form>")] for a in rows], "No emcees yet.")
        body += M("<h2>Contracts</h2>") + table(["Emcee", "Title", "Status", "Sent", ""], [[c["stage_name"], c["title"], c["status"], fmt_dt(c["created_at"]), post_button(f"/manage/{slug}/contracts/{c['id']}/void", "Void", ctx.csrf, cls="secondary") if c["status"] == "sent" else ""] for c in contracts], "No contracts.")
        body += (M(f"<form method='post' action='/manage/{h(slug)}/contracts' class='card'><h2>Send a contract</h2>") + csrf_field(ctx.csrf) + select("Emcee", "artist_id", [(a["id"], a["stage_name"]) for a in rows if a["user_id"]])
                 + field("Title", "title", required=True) + textarea("Terms", "terms", required=True, hint="Plain-language booking terms; the emcee accepts or declines in their account.") + M("<button type='submit'>Send</button></form>"))
        return page(ctx, slug, "Emcees", body)

    @r.post("/manage/{slug}/artists/{aid}/moderate")
    def moderate(ctx, slug, aid):
        guard(ctx, slug)
        status = one_of(ctx.form.get("status"), ("active", "hidden"), "status")
        n = db.run("UPDATE artists SET status=%s, moderation_note=%s WHERE id=%s AND league_slug=%s", (status, text(ctx.form.get("note"), max_len=200), integer(aid, lo=1), slug))
        if not n:
            raise HttpError(404, "Not found")
        audit(db, ctx.user.id, "artist.moderated", {"artistId": int(aid), "status": status}, slug)
        return ctx.redirect(f"/manage/{slug}/artists", "Emcee " + ("hidden." if status == "hidden" else "restored."))

    @r.post("/manage/{slug}/contracts")
    def send_contract(ctx, slug):
        guard(ctx, slug)
        a = db.one("SELECT * FROM artists WHERE id=%s AND league_slug=%s", (integer(ctx.form.get("artist_id"), lo=1), slug))
        if not a or not a["user_id"]:
            raise HttpError(400, "Unknown emcee")
        db.run("INSERT INTO contracts(league_slug,artist_id,title,terms) VALUES (%s,%s,%s,%s)", (slug, a["id"], text(ctx.form.get("title"), min_len=2, max_len=120, name="Title"), text(ctx.form.get("terms"), min_len=10, max_len=10000, name="Terms")))
        notify(db, a["user_id"], f"{slug} sent you a contract to review.")
        return ctx.redirect(f"/manage/{slug}/artists", "Contract sent.")

    @r.post("/manage/{slug}/contracts/{cid}/void")
    def void_contract(ctx, slug, cid):
        guard(ctx, slug)
        db.run("UPDATE contracts SET status='void', decided_at=now() WHERE id=%s AND league_slug=%s AND status='sent'", (integer(cid, lo=1), slug))
        return ctx.redirect(f"/manage/{slug}/artists", "Contract voided.")

    # ---------- disputes ----------
    @r.get("/manage/{slug}/disputes")
    def disputes(ctx, slug):
        guard(ctx, slug)
        rows = db.all("""SELECT d.*, m.id AS match_id, ea.display_name AS a_name, eb.display_name AS b_name, u.display_name AS raised_name FROM disputes d JOIN matches m ON m.id=d.match_id
            JOIN events e ON e.id=m.event_id LEFT JOIN entrants ea ON ea.id=m.side_a LEFT JOIN entrants eb ON eb.id=m.side_b LEFT JOIN users u ON u.id=d.raised_by WHERE e.league_slug=%s ORDER BY d.status, d.id DESC""", (slug,))
        body = M("<h1>Disputes</h1><p>Uphold a dispute after applying a correction on the battle console; corrections are audited.</p>") + table(["Battle", "Raised by", "Reason", "Status", ""], [[M(f"<a href='/manage/{h(slug)}/battles/{d['match_id']}'>{h(d['a_name'])} vs {h(d['b_name'])}</a>"), d["raised_name"] or "deleted account", d["reason"], d["status"] + (f": {d['resolution']}" if d["resolution"] else ""),
                                                                                                                                                                                                            (M(f"<form method='post' action='/manage/{h(slug)}/disputes/{d['id']}' class='row'>") + csrf_field(ctx.csrf) + M("<input name='resolution' placeholder='Resolution' aria-label='Resolution' required><select name='status' aria-label='Decision'><option value='upheld'>Uphold</option><option value='rejected'>Reject</option></select><button type='submit' class='secondary'>Resolve</button></form>")) if d["status"] == "open" else ""] for d in rows], "No disputes.")
        return page(ctx, slug, "Disputes", body)

    @r.post("/manage/{slug}/disputes/{did}")
    def resolve(ctx, slug, did):
        guard(ctx, slug)
        d = db.one("SELECT d.* FROM disputes d JOIN matches m ON m.id=d.match_id JOIN events e ON e.id=m.event_id WHERE d.id=%s AND e.league_slug=%s", (integer(did, lo=1), slug))
        if not d or d["status"] != "open":
            raise HttpError(404, "Dispute not found")
        status = one_of(ctx.form.get("status"), ("upheld", "rejected"), "decision")
        db.run("UPDATE disputes SET status=%s, resolution=%s, resolved_at=now() WHERE id=%s", (status, text(ctx.form.get("resolution"), min_len=3, max_len=500, name="Resolution"), d["id"]))
        if d["raised_by"]:
            notify(db, d["raised_by"], f"Your dispute was {status}.")
        audit(db, ctx.user.id, "dispute." + status, {"disputeId": d["id"]}, slug)
        return ctx.redirect(f"/manage/{slug}/disputes", "Dispute " + status + ".")

    # ---------- sponsorship (RAP-11) ----------
    @r.get("/manage/{slug}/sponsors")
    def sponsors(ctx, slug):
        guard(ctx, slug)
        st = db.one("SELECT sponsorship_enabled FROM league_settings WHERE league_slug=%s", (slug,))
        sps = db.all("SELECT * FROM sponsors WHERE league_slug=%s ORDER BY name", (slug,))
        pls = db.all("SELECT p.*, s.name AS sponsor_name, e.title AS event_title FROM placements p LEFT JOIN sponsors s ON s.id=p.sponsor_id LEFT JOIN events e ON e.id=p.event_id WHERE p.league_slug=%s ORDER BY p.id DESC", (slug,))
        invs = db.all("SELECT i.*, s.name AS sponsor_name FROM invoices i JOIN sponsors s ON s.id=i.sponsor_id WHERE i.league_slug=%s ORDER BY i.id DESC", (slug,))
        evs = eng.list_events(slug)
        body = M("<h1>Sponsorship</h1>") + (M("") if st and st["sponsorship_enabled"] else M(f"<p class='notice'>Sponsorship is opted out for this league; approved placements are not shown publicly until it is enabled on the <a href='/manage/{h(slug)}'>dashboard</a>.</p>"))
        body += M("<section class='card'><h2>Sponsors</h2>") + table(["Sponsor", "Website", "Contact"], [[s["name"], s["url"], s["contact_email"]] for s in sps], "No sponsors.")
        body += M(f"<form method='post' action='/manage/{h(slug)}/sponsors' class='row'>") + csrf_field(ctx.csrf) + field("Name", "name", required=True) + field("Website (https)", "url", type_="url") + field("Contact email", "contact_email") + M("<button type='submit'>Add sponsor</button></form></section>")
        body += M("<section class='card'><h2>Placement inventory and approvals</h2>") + table(["Slot", "Event", "Sponsor", "Message", "Price", "Status", "Impressions", "Clicks", ""], [[p["slot"], p["event_title"] or "League-wide", p["sponsor_name"] or "", p["message"], payments.fmt_money(p["price_cents"]), p["status"], p["impressions"], p["clicks"],
                                                                                                                                                                                join(post_button(f"/manage/{slug}/placements/{p['id']}", lbl, ctx.csrf, fields={"status": st_}, cls="secondary") for st_, lbl in (("approved", "Approve"), ("rejected", "Reject"), ("ended", "End")) if st_ != p["status"] and p["sponsor_id"])] for p in pls], "No placements.")
        body += (M(f"<form method='post' action='/manage/{h(slug)}/placements' class='row'>") + csrf_field(ctx.csrf) + select("Slot", "slot", [("league_banner", "League page banner"), ("event_banner", "Event page banner"), ("match_callout", "Battle page callout")])
                 + select("Event", "event_id", [("", "League-wide")] + [(e["id"], e["title"]) for e in evs]) + select("Sponsor", "sponsor_id", [("", "Unassigned")] + [(s["id"], s["name"]) for s in sps]) + field("Message", "message") + field("Price (USD)", "price", value="0")
                 + M("<button type='submit'>Add placement</button></form></section>"))
        body += M("<section class='card'><h2>Invoices</h2>") + table(["#", "Sponsor", "Amount", "Status", "Note", ""], [[i["id"], i["sponsor_name"], payments.fmt_money(i["amount_cents"]), i["status"], i["note"],
                                                                                                                     (M(f"<form method='post' action='/manage/{h(slug)}/invoices/{i['id']}/offline' class='row'>") + csrf_field(ctx.csrf) + M("<input name='note' placeholder='Payment reference' aria-label='Payment reference' required><button type='submit' class='secondary'>Record offline payment</button></form>")
                                                                                                                      + (M(f" <a href='/sponsor/invoices/{i['id']}'>Payment link</a>") if app.stripe.enabled else M(""))) if i["status"] == "open" else ""] for i in invs], "No invoices.")
        body += (M(f"<form method='post' action='/manage/{h(slug)}/invoices' class='row'>") + csrf_field(ctx.csrf) + select("Sponsor", "sponsor_id", [(s["id"], s["name"]) for s in sps]) + select("Placement", "placement_id", [("", "No placement")] + [(p["id"], f"#{p['id']} {p['slot']}") for p in pls])
                 + field("Amount (USD)", "amount", required=True) + M("<button type='submit'>Create invoice</button></form></section>"))
        return page(ctx, slug, "Sponsorship", body)

    @r.post("/manage/{slug}/sponsors")
    def add_sponsor(ctx, slug):
        guard(ctx, slug)
        url = text(ctx.form.get("url"), max_len=300)
        if url and not url.startswith("https://"):
            raise HttpError(400, "Website must be an https link")
        db.run("INSERT INTO sponsors(league_slug,name,url,contact_email) VALUES (%s,%s,%s,%s)", (slug, text(ctx.form.get("name"), min_len=1, max_len=120, name="Name"), url, text(ctx.form.get("contact_email"), max_len=200)))
        return ctx.redirect(f"/manage/{slug}/sponsors", "Sponsor added.")

    @r.post("/manage/{slug}/placements")
    def add_placement(ctx, slug):
        guard(ctx, slug)
        eid = integer(ctx.form.get("event_id"), lo=1, default=0) or None
        if eid:
            eng.event(eid, slug)
        sid = integer(ctx.form.get("sponsor_id"), lo=1, default=0) or None
        if sid and not db.one("SELECT 1 FROM sponsors WHERE id=%s AND league_slug=%s", (sid, slug)):
            raise HttpError(400, "Unknown sponsor")
        db.run("INSERT INTO placements(league_slug,slot,event_id,sponsor_id,message,price_cents,status) VALUES (%s,%s,%s,%s,%s,%s,%s)",
               (slug, one_of(ctx.form.get("slot"), ("league_banner", "event_banner", "match_callout"), "slot"), eid, sid, text(ctx.form.get("message"), max_len=140), money(ctx.form.get("price"), name="Price"), "requested" if sid else "available"))
        return ctx.redirect(f"/manage/{slug}/sponsors", "Placement added.")

    @r.post("/manage/{slug}/placements/{pid}")
    def placement_status(ctx, slug, pid):
        guard(ctx, slug)
        status = one_of(ctx.form.get("status"), ("approved", "rejected", "ended"), "status")
        if not db.run("UPDATE placements SET status=%s WHERE id=%s AND league_slug=%s AND sponsor_id IS NOT NULL", (status, integer(pid, lo=1), slug)):
            raise HttpError(404, "Placement not found")
        audit(db, ctx.user.id, "placement." + status, {"placementId": int(pid)}, slug)
        return ctx.redirect(f"/manage/{slug}/sponsors", "Placement " + status + ".")

    @r.post("/manage/{slug}/invoices")
    def add_invoice(ctx, slug):
        guard(ctx, slug)
        sid = integer(ctx.form.get("sponsor_id"), lo=1)
        if not db.one("SELECT 1 FROM sponsors WHERE id=%s AND league_slug=%s", (sid, slug)):
            raise HttpError(400, "Unknown sponsor")
        pid = integer(ctx.form.get("placement_id"), lo=1, default=0) or None
        db.run("INSERT INTO invoices(league_slug,sponsor_id,placement_id,amount_cents) VALUES (%s,%s,%s,%s)", (slug, sid, pid, money(ctx.form.get("amount"), name="Amount", allow_zero=False)))
        return ctx.redirect(f"/manage/{slug}/sponsors", "Invoice created.")

    @r.post("/manage/{slug}/invoices/{iid}/offline")
    def invoice_offline(ctx, slug, iid):
        guard(ctx, slug)
        inv = db.one("SELECT * FROM invoices WHERE id=%s AND league_slug=%s AND status='open'", (integer(iid, lo=1), slug))
        if not inv:
            raise HttpError(404, "Invoice not found")
        o = payments.create_order(db, league=slug, kind="sponsor", amount_cents=inv["amount_cents"], ref_id=inv["id"])
        payments.mark_paid(db, o["id"])
        db.run("UPDATE invoices SET note=%s WHERE id=%s", ("Offline: " + text(ctx.form.get("note"), min_len=2, max_len=120, name="Payment reference"), inv["id"]))
        audit(db, ctx.user.id, "invoice.offline_paid", {"invoiceId": inv["id"], "orderId": o["id"]}, slug)
        return ctx.redirect(f"/manage/{slug}/sponsors", "Offline payment recorded and split.")

    # ---------- revenue (RAP-10) ----------
    @r.get("/manage/{slug}/revenue")
    def revenue(ctx, slug):
        guard(ctx, slug)
        rules = [payments.split_rule(db, slug, k) for k in payments.KINDS]
        totals = payments.ledger_totals(db, slug)
        orders = db.all("SELECT * FROM orders WHERE league_slug=%s AND status<>'pending' ORDER BY id DESC LIMIT 100", (slug,))
        sets = db.all("SELECT * FROM settlements WHERE league_slug=%s ORDER BY id DESC", (slug,))
        body = M(f"<h1>Revenue</h1><p class='{'' if app.stripe.enabled else 'notice'}'>{'Online payments are on (Stripe Checkout).' if app.stripe.enabled else 'Online payments are OFF. A platform admin enables them by setting STRIPE_SECRET_KEY and STRIPE_WEBHOOK_SECRET. Offline sponsor payments can still be recorded.'}</p>")
        body += M(f"<form method='post' action='/manage/{h(slug)}/splits' class='card'><h2>Revenue splits (% per kind)</h2>") + csrf_field(ctx.csrf) + table(["Kind", "Platform", "League", "Artist", "Event pool"], [[payments.KIND_LABEL[x["kind"]] + (" (default)" if x.get("default") else "")] + [M(f"<input name='{x['kind']}_{p}' value='{x[p + '_bps'] / 100:g}' size='5' aria-label='{x['kind']} {p}'>") for p in ("platform", "league", "artist", "event_pool")] for x in rules]) + M("<p class='muted'>Each row must total 100. Event-pool shares are divided equally among the event's emcees at settlement; artists without payout consent are held.</p><button type='submit'>Save splits</button></form>")
        body += M("<section class='card'><h2>Ledger totals (all time)</h2>") + table(["Party", "Entry", "Lines", "Amount"], [[t["party"], t["entry"], t["n"], payments.fmt_money(t["cents"])] for t in totals], "No revenue yet.") + M("</section>")
        body += M("<section class='card'><h2>Orders</h2>") + table(["#", "Kind", "Amount", "Refunded", "Status", "Paid", ""], [[o["id"], payments.KIND_LABEL[o["kind"]], payments.fmt_money(o["amount_cents"]), payments.fmt_money(o["refunded_cents"]), o["status"], fmt_dt(o["paid_at"]),
                                                                                                                          post_button(f"/manage/{slug}/orders/{o['id']}/refund", "Refund via Stripe", ctx.csrf, cls="secondary", confirm="Refund this order in full?") if app.stripe.enabled and o["stripe_payment_intent"] and o["status"] == "paid" else ""] for o in orders], "No orders.") + M("</section>")
        body += M("<section class='card'><h2>Settlements</h2>") + post_button(f"/manage/{slug}/settle", "Create settlement from unsettled lines", ctx.csrf) + table(["#", "Created", "Lines", "Platform", "League", "Artists", "Held", "Evidence SHA-256"], [[s["id"], fmt_dt(s["created_at"]), s["entry_count"], payments.fmt_money(s["totals"]["platform"]), payments.fmt_money(s["totals"]["league"]),
                                                                                                                                                                                                                                          "; ".join(f"{k.split(':', 1)[1]} {payments.fmt_money(v)}" for k, v in s["totals"]["artists"].items()), "; ".join(f"{k.split(':', 1)[1]} {payments.fmt_money(v)}" for k, v in s["totals"]["held"].items()), M(f"<code>{h(s['evidence_sha256'][:16])}…</code>")] for s in sets], "No settlements.") + M("</section>")
        return page(ctx, slug, "Revenue", body)

    @r.post("/manage/{slug}/splits")
    def save_splits(ctx, slug):
        guard(ctx, slug)
        for k in payments.KINDS:
            vals = []
            for p in ("platform", "league", "artist", "event_pool"):
                try:
                    vals.append(round(float(ctx.form.get(f"{k}_{p}", "0")) * 100))
                except ValueError:
                    raise HttpError(400, "Shares must be numbers")
            payments.set_split_rule(db, slug, k, *vals)
        audit(db, ctx.user.id, "splits.updated", {}, slug)
        return ctx.redirect(f"/manage/{slug}/revenue", "Revenue splits saved.")

    @r.post("/manage/{slug}/settle")
    def settle(ctx, slug):
        guard(ctx, slug)
        s = payments.settle(db, slug, ctx.user.id)
        audit(db, ctx.user.id, "settlement.created", {"settlementId": s["id"], "evidence": s["evidence_sha256"]}, slug)
        return ctx.redirect(f"/manage/{slug}/revenue", f"Settlement #{s['id']} created.")

    @r.post("/manage/{slug}/orders/{oid}/refund")
    def refund(ctx, slug, oid):
        guard(ctx, slug)
        o = db.one("SELECT * FROM orders WHERE id=%s AND league_slug=%s", (integer(oid, lo=1), slug))
        if not o or o["status"] != "paid" or not o["stripe_payment_intent"]:
            raise HttpError(409, "Only paid Stripe orders can be refunded here")
        if not app.stripe.enabled:
            raise HttpError(409, "Payments are off")
        app.stripe.refund(o["stripe_payment_intent"])
        audit(db, ctx.user.id, "order.refund_requested", {"orderId": o["id"]}, slug)
        return ctx.redirect(f"/manage/{slug}/revenue", "Refund requested; the ledger updates when Stripe confirms it.")

