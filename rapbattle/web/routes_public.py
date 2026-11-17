"""Public pages: platform home, league discovery (RAP-08), league pages, battles, voting, standings (RAP-09)."""
from .http import HttpError, h, join, integer, text, Response
from .views import M, table, fmt_dt, fmt_ms, post_button, embed, select, field
from .settings import privacy_gaps
from ..engine import scoring
from .. import payments

STATUS_LABEL = {"draft": "Draft", "scheduled": "Scheduled", "live": "Live now", "completed": "Results final", "cancelled": "Cancelled"}
MATCH_LABEL = {"scheduled": "Scheduled", "in_progress": "In progress", "judging": "Judging", "overtime": "Overtime", "decided": "Decided",
               "draw": "Draw", "forfeited": "Forfeit", "cancelled": "Cancelled"}


def event_card(e):
    return M(f"<li class='card'><span class='pill'>{h(STATUS_LABEL.get(e['status'], e['status']))}</span><h3><a href='/l/{h(e['league_slug'])}/events/{h(e['slug'])}'>{h(e['title'])}</a></h3>"
             f"<p class='muted'>{h(fmt_dt(e['starts_at']) or 'Date to be announced')}{(' · ' + h(e['venue'])) if e['venue'] else ''}</p></li>")


def placements(app, league, slot, event_id=None):
    st = app.db.one("SELECT sponsorship_enabled FROM league_settings WHERE league_slug=%s", (league,))
    if not st or not st["sponsorship_enabled"]:
        return M("")
    rows = app.db.all("""SELECT p.id, p.message, s.name FROM placements p JOIN sponsors s ON s.id=p.sponsor_id WHERE p.league_slug=%s AND p.slot=%s AND p.status='approved'
        AND (p.event_id IS NULL OR p.event_id=%s) ORDER BY p.id""", (league, slot, event_id))
    if not rows:
        return M("")
    app.db.run("UPDATE placements SET impressions=impressions+1 WHERE id = ANY(%s)", ([r["id"] for r in rows],))
    return join(M(f"<aside class='sponsor' aria-label='Sponsor'><span class='muted'>Sponsored by</span> <a href='/sp/{r['id']}' rel='sponsored noopener'>{h(r['name'])}</a>{(' — ' + h(r['message'])) if r['message'] else ''}</aside>") for r in rows)


def match_card(m, league, *, link=True):
    a, b = h(m["a_name"] or "TBD"), h(m["b_name"] or "TBD")
    w = m["winner"]
    res = ""
    if m["status"] in ("decided", "forfeited") and w:
        res = f"<p>Winner: <strong>{h(m['a_name'] if w == m['side_a'] else m['b_name'])}</strong> <span class='muted'>({h(m['outcome_reason'])})</span></p>"
    elif m["status"] == "draw":
        res = "<p>Draw</p>"
    title = f"<a href='/l/{h(league)}/battles/{m['id']}'>{a} vs {b}</a>" if link else f"{a} vs {b}"
    return M(f"<div class='card'><span class='pill'>{h(MATCH_LABEL.get(m['status'], m['status']))}</span><h3>{title}</h3>{res}</div>")


def mount(r, app):
    eng, db = app.engine, app.db

    @r.get("/")
    def home(ctx):
        leagues = db.all("""SELECT l.slug, l.name, l.synthetic, s.city, s.region FROM leagues l LEFT JOIN league_settings s ON s.league_slug=l.slug
            WHERE COALESCE(s.listed, true) ORDER BY l.name LIMIT 12""")
        live = eng.list_events(published_only=True, statuses=["live", "scheduled"], limit=12)
        body = M("<p class='eyebrow'>Independent leagues. One stage.</p><h1>Rap battles, run by the leagues that host them.</h1>"
                 "<p>Find a league near you, watch battles live, vote when the crowd counts, and follow the standings.</p>"
                 "<p class='row'><a class='button' href='/leagues'>Find a league</a></p>")
        body += M("<h2>Upcoming and live</h2>") + (M("<ul class='grid'>") + join(event_card(e) for e in live) + M("</ul>") if live else M("<p class='muted'>No battles are published yet.</p>"))
        body += M("<h2>Leagues</h2><ul class='grid'>") + join(M(f"<li class='card'><h3><a href='/l/{h(l['slug'])}'>{h(l['name'])}</a></h3><p class='muted'>{h(', '.join(x for x in (l['city'], l['region']) if x) or 'Location not listed')}{' · synthetic fixture' if l['synthetic'] else ''}</p></li>") for l in leagues) + M("</ul>")
        return ctx.page("Rap Battle", body)

    @r.get("/leagues")
    def discover(ctx):
        q = text(ctx.req.query.get("q"), max_len=80)
        region = text(ctx.req.query.get("region"), max_len=80)
        cond, p = ["COALESCE(s.listed, true)"], []
        if q:
            cond.append("(l.name ILIKE %s OR s.city ILIKE %s OR s.description ILIKE %s)")
            p += ["%" + q.replace("%", r"\%").replace("_", r"\_") + "%"] * 3
        if region:
            cond.append("(s.region ILIKE %s OR s.city ILIKE %s)")
            p += ["%" + region.replace("%", r"\%").replace("_", r"\_") + "%"] * 2
        rows = db.all(f"""SELECT l.slug, l.name, l.synthetic, s.city, s.region, s.description,
            (SELECT count(*) FROM events e WHERE e.league_slug=l.slug AND e.published AND e.status IN ('scheduled','live')) AS upcoming
            FROM leagues l LEFT JOIN league_settings s ON s.league_slug=l.slug WHERE {' AND '.join(cond)} ORDER BY l.name""", p)
        body = M("<p class='eyebrow'>League discovery</p><h1>Find a league</h1><p>Browse every listed league from anywhere — watch, vote and follow without living nearby.</p>"
                 "<form method='get' action='/leagues' class='row card'>") + field("Search", "q", value=q) + field("City or region", "region", value=region) + M("<button type='submit'>Search</button></form>")
        body += table(["League", "Location", "Upcoming battles", ""], [[M(f"<a href='/l/{h(x['slug'])}'>{h(x['name'])}</a>"), ", ".join(y for y in (x["city"], x["region"]) if y), str(x["upcoming"]),
                                                                       "Synthetic fixture" if x["synthetic"] else ""] for x in rows], "No listed league matches.")
        return ctx.page("Leagues", body)

    @r.get("/privacy")
    def privacy(ctx):
        s = app.settings.all()
        gaps = privacy_gaps(s)
        if gaps:
            body = M("<h1>Privacy</h1><section class='card'><h2>The privacy policy is not yet published</h2><p>Artist sign-up is closed and no artist profile data is collected until the operator publishes it.</p></section>")
        else:
            paras = join(M(f"<p>{h(p)}</p>") for p in s["policy_text"].split("\n\n") if p.strip())
            body = M(f"<h1>Privacy policy</h1><section class='card'><dl><dt>Operator</dt><dd>{h(s['operator_name'])}</dd><dt>Privacy contact</dt><dd>{h(s['privacy_contact'])}</dd>"
                     f"<dt>Retention</dt><dd>Deleted profiles and photos are removed immediately; sign-in and moderation audit records and notifications are kept {h(s['retention_days'])} days.</dd><dt>Policy version</dt><dd>{h(s['policy_version'])}</dd></dl></section><section class='card'>{paras}</section>")
        body += M("<section class='card'><h2>Your controls</h2><ul><li>Profile publication, media display and payout consent are separate choices you can change in your account.</li>"
                  "<li>You can delete your account and artist profiles at any time.</li><li>Votes store a keyed hash of your network address only to enforce fair-voting limits.</li></ul></section>")
        return ctx.page("Privacy", body)

    @r.get("/l/{slug}")
    def league_home(ctx, slug):
        lg = app.league(slug)
        brand = app.brand(slug)
        st = db.one("SELECT * FROM league_settings WHERE league_slug=%s", (slug,)) or {}
        events = eng.list_events(slug, published_only=True, statuses=["live", "scheduled"])
        champs = eng.champions(slug)[:5]
        top = eng.standings(slug)[:5]
        body = M(f"<p class='eyebrow'>{h(brand['tagline'] if brand else '')}</p><h1>{h(brand['display_name'] if brand else lg['name'])}</h1>")
        if st.get("description"):
            body += M(f"<p>{h(st['description'])}</p>")
        body += placements(app, slug, "league_banner")
        body += M("<h2>Upcoming battles</h2>") + (M("<ul class='grid'>") + join(event_card(e) for e in events) + M("</ul>") if events else M("<p class='muted'>No upcoming battles are published.</p>"))
        body += M("<h2>Standings</h2>") + table(["#", "Emcee", "W", "L", "D", "Pts"], [[str(x["rank"]), M(f"<a href='/l/{h(slug)}/artists/{x['artist_id']}'>{h(x['name'])}</a>"), x["wins"], x["losses"], x["draws"], x["points"]] for x in top], "No decided battles yet.")
        body += M("<h2>Champions</h2>") + table(["Event", "Champion", "Crowned"], [[M(f"<a href='/l/{h(slug)}/events/{h(c['slug'])}'>{h(c['title'])}</a>"), c["display_name"], fmt_dt(c["finalized_at"])] for c in champs], "No champions yet.")
        body += M(f"<p class='row'><a href='/l/{h(slug)}/events'>All battles and archive</a> <a href='/l/{h(slug)}/artists'>Emcee roster</a> <a href='/artist/signup?league={h(slug)}'>Join as an emcee</a></p>")
        notice = "Synthetic fixture: this league, its branding and its formats are fictional test data." if lg["synthetic"] else None
        return ctx.page(brand["display_name"] if brand else lg["name"], body, league=slug, brand=brand, notice=notice)

    @r.get("/l/{slug}/events")
    def league_events(ctx, slug):
        app.league(slug)
        events = eng.list_events(slug, published_only=True)
        upcoming = [e for e in events if e["status"] in ("live", "scheduled")]
        past = [e for e in events if e["status"] in ("completed", "cancelled")]
        body = (M("<h1>Battles</h1><h2>Calendar</h2>") + (M("<ul class='grid'>") + join(event_card(e) for e in sorted(upcoming, key=lambda x: str(x["starts_at"] or ""))) + M("</ul>") if upcoming else M("<p class='muted'>Nothing scheduled.</p>"))
                + M("<h2>Archive</h2>") + (M("<ul class='grid'>") + join(event_card(e) for e in past) + M("</ul>") if past else M("<p class='muted'>No past events.</p>")))
        return ctx.page("Battles", body, league=slug)

    @r.get("/l/{slug}/events/{event}")
    def event_page(ctx, slug, event):
        app.league(slug)
        e = eng.event_by_slug(slug, event)
        if not e or not e["published"]:
            raise HttpError(404, "Event not found")
        ms = eng.matches(e["id"])
        cfg = e["config"]
        stages = sorted({m["stage"] for m in ms})
        live = e["status"] == "live"
        body = M(f"<p class='eyebrow'>{h(STATUS_LABEL.get(e['status'], e['status']))}{' · synthetic fixture' if e['synthetic'] else ''}</p><h1>{h(e['title'])}</h1>"
                 f"<p>{h(fmt_dt(e['starts_at']))}{(' · ' + h(e['venue'])) if e['venue'] else ''}</p><p class='muted'>Format: {h(e['preset_id'])} · {cfg['rounds']} round(s) × {cfg['round_seconds']}s · {h(cfg['style'])} · tie rule {h(cfg['tie_policy'])}</p>")
        if e["description"]:
            body += M(f"<p>{h(e['description'])}</p>")
        if e["champion_entrant_id"]:
            champ = db.one("SELECT display_name FROM entrants WHERE id=%s", (e["champion_entrant_id"],))
            body += M(f"<p class='card'><strong>Champion:</strong> {h(champ['display_name'])}</p>")
        body += placements(app, slug, "event_banner", e["id"])
        body += purchase_block(ctx, e)
        if e["stream_url"] and (e["ppv_cents"] == 0 or payments.has_access(db, ctx.user and ctx.user.id, kind="ppv", event_id=e["id"])):
            body += M("<h2>Stream</h2>") + embed(e["stream_url"], "Stream of " + e["title"])
        if stages and len(stages) > 1:
            body += M("<h2>Bracket</h2><div class='bracket'>") + join(M(f"<section><h3>Stage {s}</h3>") + join(match_card(m, slug) for m in ms if m["stage"] == s) + M("</section>") for s in stages) + M("</div>")
        else:
            body += M("<h2>Battle card</h2>") + (join(match_card(m, slug) for m in ms) if ms else M("<p class='muted'>Battles will be announced.</p>"))
        return ctx.page(e["title"], body, league=slug, refresh=20 if live else None)

    def purchase_block(ctx, e):
        out = M("")
        for kind, cents in (("ticket", e["ticket_cents"]), ("ppv", e["ppv_cents"])):
            if not cents or e["status"] in ("completed", "cancelled"):
                continue
            label = f"{payments.KIND_LABEL[kind]} — {payments.fmt_money(cents)}"
            if payments.has_access(db, ctx.user and ctx.user.id, kind=kind, event_id=e["id"]):
                out += M(f"<p class='pill'>You have a {h(payments.KIND_LABEL[kind].lower())}.</p> ")
            elif not app.stripe.enabled:
                out += M(f"<p class='muted'>{h(label)}: online payments are switched off for this platform.</p>")
            else:
                out += post_button("/pay/checkout", "Buy " + label, ctx.csrf, fields={"kind": kind, "event_id": e["id"]})
        return out

    @r.get("/l/{slug}/battles/{mid}")
    def battle_page(ctx, slug, mid):
        m = eng.match(integer(mid, lo=1), slug)
        if not m["published"]:
            raise HttpError(404, "Battle not found")
        cfg = m["config"]
        body = M(f"<p class='eyebrow'><a href='/l/{h(slug)}/events/{h(m['event_slug'])}'>{h(m['event_title'])}</a> · stage {m['stage']}</p>"
                 f"<div class='versus'><strong>{h(m['a_name'] or 'TBD')}</strong><span>vs</span><strong>{h(m['b_name'] or 'TBD')}</strong></div>"
                 f"<p><span class='pill'>{h(MATCH_LABEL[m['status']])}</span> Round {m['current_round'] or '-'} of {cfg['rounds']}{(' + ' + str(m['overtimes']) + ' overtime') if m['overtimes'] else ''}</p>")
        if m["status"] in ("in_progress", "overtime"):
            limit = cfg["overtime_seconds"] if m["current_round"] > cfg["rounds"] else cfg["round_seconds"]
            t = scoring.timer_state(m, limit)
            who = m["a_name"] if m["timer_side"] == "a" else m["b_name"] if m["timer_side"] == "b" else None
            body += M(f"<section class='card' aria-live='polite'><h2>{h('On the mic: ' + who if who else 'Between turns')}</h2><p class='timer{' over' if t['remaining_ms'] < 0 else ''}' data-remaining-ms='{t['remaining_ms']}' data-running='{1 if t['running'] else 0}'>{fmt_ms(t['remaining_ms'])}</p></section>")
        body += placements(app, slug, "match_callout", m["event_id"])
        if m["voting_open"]:
            if ctx.user:
                body += M("<section class='card'><h2>Crowd vote</h2><p>One vote per account.</p><div class='row'>") + post_button(f"/l/{slug}/battles/{m['id']}/vote", "Vote " + (m["a_name"] or "A"), ctx.csrf, fields={"side": "a"}) + post_button(f"/l/{slug}/battles/{m['id']}/vote", "Vote " + (m["b_name"] or "B"), ctx.csrf, fields={"side": "b"}) + M("</div></section>")
            else:
                body += M(f"<p class='notice'><a href='/login?next=/l/{h(slug)}/battles/{m['id']}'>Sign in</a> to vote.</p>")
        if m["status"] in ("decided", "draw", "forfeited"):
            vc = eng.vote_counts(m["id"])
            body += M(f"<p>Crowd votes: {h(m['a_name'])} {vc['a']} · {h(m['b_name'])} {vc['b']}</p>")
            if m["recording_url"]:
                body += M("<h2>Recording</h2>") + embed(m["recording_url"], "Battle recording")
            if ctx.user:
                body += M(f"<details class='card'><summary>Dispute this result</summary><form method='post' action='/l/{h(slug)}/battles/{m['id']}/dispute'><input type='hidden' name='_csrf' value='{h(ctx.csrf)}'>"
                          "<label class='field'><span>Reason *</span><textarea name='reason' required maxlength='1000'></textarea></label><button type='submit'>Submit dispute</button></form></details>")
        return ctx.page(f"{m['a_name'] or 'TBD'} vs {m['b_name'] or 'TBD'}", body, league=slug, refresh=15 if m["status"] in ("in_progress", "overtime", "judging") else None)

    @r.post("/l/{slug}/battles/{mid}/vote")
    def vote(ctx, slug, mid):
        if not ctx.user:
            return ctx.redirect(f"/login?next=/l/{slug}/battles/{mid}", {"kind": "error", "text": "Sign in to vote."})
        if not app.limiter.allow("vote:" + ctx.ip_hash, 30, 60):
            raise HttpError(429, "Too many votes from this network; wait a minute")
        m = eng.match(integer(mid, lo=1), slug)
        s = app.settings.all()
        eng.cast_vote(user=ctx.user, match_id=m["id"], side=ctx.form.get("side"), ip_hash=ctx.ip_hash, min_account_minutes=s["vote_min_account_minutes"], ip_cap=s["vote_ip_cap"])
        return ctx.redirect(f"/l/{slug}/battles/{m['id']}", "Vote recorded.")

    @r.post("/l/{slug}/battles/{mid}/dispute")
    def dispute(ctx, slug, mid):
        ctx.require()
        m = eng.match(integer(mid, lo=1), slug)
        if m["status"] not in ("decided", "draw", "forfeited"):
            raise HttpError(409, "Only finished battles can be disputed")
        if not app.limiter.allow("dispute:" + str(ctx.user.id), 5, 86400):
            raise HttpError(429, "Daily dispute limit reached")
        db.run("INSERT INTO disputes(match_id,raised_by,reason) VALUES (%s,%s,%s)", (m["id"], ctx.user.id, text(ctx.form.get("reason"), min_len=10, max_len=1000, name="Reason")))
        return ctx.redirect(f"/l/{slug}/battles/{m['id']}", "Dispute submitted to the league.")

    @r.get("/l/{slug}/standings")
    def standings_page(ctx, slug):
        app.league(slug)
        seasons = db.all("SELECT * FROM seasons WHERE league_slug=%s ORDER BY id DESC", (slug,))
        sid = integer(ctx.req.query.get("season") or 0, lo=0, default=0) or None
        style = ctx.req.query.get("style") or ""
        rows_all = [x for x in eng.league_results(slug, sid) if not style or x["style"] == style]
        pts = next(({"win": s["points_win"], "draw": s["points_draw"], "loss": s["points_loss"]} for s in seasons if s["id"] == sid), {"win": 3, "draw": 1, "loss": 0})
        st = scoring.standings([x for x in rows_all if x["a"] and x["b"]], **pts)
        body = (M("<h1>Standings and records</h1><form method='get' class='row card'>") + select("Season", "season", [("", "All time")] + [(s["id"], s["name"]) for s in seasons], sid or "")
                + select("Style", "style", [("", "All styles"), ("written", "Written"), ("freestyle", "Freestyle")], style) + M("<button type='submit'>Filter</button></form>"))
        body += table(["#", "Emcee", "Played", "W", "L", "D", "Pts"], [[str(x["rank"]), M(f"<a href='/l/{h(slug)}/artists/{x['artist_id']}'>{h(x['name'])}</a>"), x["played"], x["wins"], x["losses"], x["draws"], x["points"]] for x in st], "No decided battles yet.")
        body += M("<h2>Results</h2>") + table(["Date", "Event", "Battle", "Result"], [[fmt_dt(x["decided_at"]), x["event_title"], f"{x['a_name']} vs {x['b_name']}",
                                                                                     "Draw" if x["status"] == "draw" else (f"{x['a_name'] if x['winner'] == x['a'] else x['b_name']} ({x['outcome_reason']})")] for x in rows_all[:100]], "No results yet.")
        return ctx.page("Standings", body, league=slug)

    @r.get("/sp/{pid}")
    def sponsor_click(ctx, pid):
        p = db.one("SELECT p.id, s.url FROM placements p JOIN sponsors s ON s.id=p.sponsor_id WHERE p.id=%s AND p.status='approved'", (integer(pid, lo=1),))
        if not p or not p["url"]:
            raise HttpError(404, "Not found")
        db.run("UPDATE placements SET clicks=clicks+1 WHERE id=%s", (p["id"],))
        return Response(303, "", "text/plain", [("Location", p["url"])])

    @r.get("/uploads/{uid}")
    def upload(ctx, uid):
        from .routes_artists import can_view
        a = db.one("SELECT * FROM artists WHERE photo_id=%s", (integer(uid, lo=1),))
        if not a or not a["consent_media"] or not can_view(ctx.user, a):
            raise HttpError(404, "Not found")
        u = db.one("SELECT * FROM uploads WHERE id=%s", (a["photo_id"],))
        if not u:
            raise HttpError(404, "Not found")
        return Response(200, bytes(u["data"]), u["mime"], [("Cache-Control", "private, max-age=300"), ("Content-Security-Policy", "default-src 'none'; sandbox")])
