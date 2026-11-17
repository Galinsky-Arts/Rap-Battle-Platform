"""RAP-05 artist profiles with consent capture, RAP-06 creator economy (tips, memberships, paid media),
lineup applications. Artist sign-up is gated behind the admin-entered privacy policy."""
from psycopg.types.json import Jsonb

from .http import HttpError, text, integer, checkbox, one_of, money
from .views import M, field, textarea, select, check, csrf_field, table, post_button, h, join, embed, fmt_dt
from .media import media_link, validate_image
from .settings import privacy_gaps
from ..web.db import audit, notify, UniqueViolation
from .. import payments

STYLES = [("both", "Written and freestyle"), ("written", "Written"), ("freestyle", "Freestyle")]
VISIBILITY = [("public", "Public profile"), ("promoters", "Visible to this league's promoters only"), ("private", "Private (only me)")]


def can_view(user, artist):
    if user and (user.id == artist["user_id"] or user.produces(artist["league_slug"])):
        return True
    return artist["status"] == "active" and artist["consent_profile"] and artist["visibility"] == "public"


def mount(r, app):
    db, eng = app.db, app.engine

    def artist(slug, aid):
        a = db.one("SELECT * FROM artists WHERE id=%s AND league_slug=%s", (integer(aid, lo=1), slug))
        if not a:
            raise HttpError(404, "Emcee not found")
        return a

    def own(ctx, slug, aid):
        a = artist(slug, aid)
        if not ctx.require() or a["user_id"] != ctx.user.id:
            raise HttpError(404, "Emcee not found")
        return a

    def links_from(form):
        return [x for x in (media_link(form.get(f"link{i}"), name=f"Media link {i}") for i in (1, 2, 3)) if x]

    def store_photo(ctx, file):
        s = app.settings.all()
        img = validate_image(file, s["upload_max_kb"] * 1024)
        if not img:
            return None
        return db.one("INSERT INTO uploads(owner_user_id,mime,size,data) VALUES (%s,%s,%s,%s) RETURNING id", (ctx.user.id, img["mime"], len(img["data"]), img["data"]))["id"]

    @r.get("/artist/signup")
    def signup_form(ctx):
        s = app.settings.all()
        if not s["artist_signup_open"]:
            gaps = privacy_gaps(s)
            extra = M(f"<p class='notice'>Admin: sign-up is closed. {h('Set ' + ', '.join(gaps) + ' in ' if gaps else 'Switch it on in ')}<a href='/admin/settings'>Settings</a>.</p>") if ctx.user and ctx.user.is_admin else M("")
            return ctx.page("Emcee sign-up", M("<h1>Emcee sign-up is not open</h1><p>Artist profiles open once the platform publishes its privacy policy. Do not send personal details by other means.</p>") + extra)
        if not ctx.user:
            return ctx.redirect("/login?next=/artist/signup", {"text": "Sign in or create an account first."})
        leagues = db.all("SELECT l.slug, l.name FROM leagues l LEFT JOIN league_settings s ON s.league_slug=l.slug WHERE COALESCE(s.listed,true) ORDER BY l.name")
        kb = s["upload_max_kb"]
        body = (M("<h1>Create your emcee profile</h1><form method='post' action='/artist/signup' enctype='multipart/form-data' class='card'>") + csrf_field(ctx.csrf)
                + select("League", "league", [(l["slug"], l["name"]) for l in leagues], ctx.req.query.get("league", ""))
                + field("Stage name", "stage_name", required=True, value=ctx.user.name, attrs="maxlength='60'") + field("Hometown", "hometown", attrs="maxlength='80'")
                + select("Style", "style", STYLES, "both") + textarea("Bio", "bio", hint="Up to 1,500 characters.")
                + field("Media link 1", "link1", type_="url", hint="YouTube, Vimeo or SoundCloud.") + field("Media link 2", "link2", type_="url") + field("Media link 3", "link3", type_="url")
                + field("Photo (optional)", "photo", type_="file", hint=f"JPEG, PNG or WebP up to {kb} KB.", attrs="accept='image/jpeg,image/png,image/webp'")
                + select("Profile visibility", "visibility", VISIBILITY, "public")
                + M(f"<section class='card'><h2>Consent (policy {h(s['policy_version'])})</h2><p>{h(s['operator_name'])} runs this platform. Privacy contact: {h(s['privacy_contact'])}. Read the <a href='/privacy'>privacy policy</a>.</p>")
                + check("I agree that my profile (stage name, hometown, style, bio and battle record) is published according to my visibility choice (required).", "consent_profile", required=True)
                + check("Show my photo and media links on my profile.", "consent_media", hint="Optional; you can change this later.")
                + M("</section><button type='submit'>Create profile</button></form>"))
        return ctx.page("Emcee sign-up", body)

    @r.post("/artist/signup", multipart=True)
    def signup(ctx):
        u = ctx.require()
        s = app.settings.all()
        if not s["artist_signup_open"]:
            raise HttpError(409, "Emcee sign-up is not open")
        if not checkbox(ctx.form.get("consent_profile")):
            raise HttpError(400, "Profile consent is required to create a profile")
        slug = ctx.form.get("league") or ""
        app.league(slug)
        links = links_from(ctx.form)
        photo = store_photo(ctx, ctx.req.files.get("photo"))
        try:
            a = db.one("""INSERT INTO artists(league_slug,user_id,stage_name,bio,hometown,style,media_links,photo_id,visibility,consent_version,consent_profile,consent_media,consent_ip_hash)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,true,%s,%s) RETURNING id""",
                       (slug, u.id, text(ctx.form.get("stage_name"), min_len=1, max_len=60, name="Stage name"), text(ctx.form.get("bio"), max_len=1500, name="Bio"),
                        text(ctx.form.get("hometown"), max_len=80), one_of(ctx.form.get("style"), [x[0] for x in STYLES], "style"), Jsonb(links), photo,
                        one_of(ctx.form.get("visibility"), [x[0] for x in VISIBILITY], "visibility"), s["policy_version"], checkbox(ctx.form.get("consent_media")), ctx.ip_hash))
        except UniqueViolation:
            raise HttpError(409, "You already have a profile in that league")
        audit(db, u.id, "artist.created", {"artistId": a["id"], "policyVersion": s["policy_version"]}, slug)
        for p in db.all("SELECT user_id FROM league_members WHERE league_slug=%s AND role='producer'", (slug,)):
            notify(db, p["user_id"], f"New emcee profile in {slug}.")
        return ctx.redirect(f"/l/{slug}/artists/{a['id']}", "Profile created.")

    @r.get("/l/{slug}/artists")
    def roster(ctx, slug):
        app.league(slug)
        promoter = bool(ctx.user and ctx.user.produces(slug))
        rows = db.all(f"""SELECT * FROM artists WHERE league_slug=%s AND consent_profile AND status='active' AND (visibility='public' {"OR visibility='promoters'" if promoter else ""}) ORDER BY stage_name""", (slug,))
        body = M("<h1>Emcee roster</h1>") + (M("<ul class='grid'>") + join(M(f"<li class='card'><h3><a href='/l/{h(slug)}/artists/{a['id']}'>{h(a['stage_name'])}</a></h3><p class='muted'>{h(a['hometown'])} · {h(dict(STYLES)[a['style']])}{' · promoters only' if a['visibility'] == 'promoters' else ''}</p></li>") for a in rows) + M("</ul>") if rows else M("<p class='muted'>No public emcee profiles yet.</p>"))
        return ctx.page("Emcees", body + M(f"<p><a href='/artist/signup?league={h(slug)}'>Create your emcee profile</a></p>"), league=slug)

    @r.get("/l/{slug}/artists/{aid}")
    def profile(ctx, slug, aid):
        a = artist(slug, aid)
        if not can_view(ctx.user, a):
            raise HttpError(404, "Emcee not found")
        results = [x for x in eng.league_results(slug) if a["id"] in (x["a"], x["b"])]
        wins = sum(1 for x in results if x["winner"] == a["id"])
        draws = sum(1 for x in results if x["status"] == "draw")
        body = M(f"<p class='eyebrow'>Emcee</p><h1>{h(a['stage_name'])}</h1><p>{h(a['hometown'])} · {h(dict(STYLES)[a['style']])}</p>")
        if a["status"] == "hidden":
            body += M("<p class='notice'>This profile is hidden by the league's moderators.</p>")
        if a["consent_media"] and a["photo_id"]:
            body += M(f"<p><img src='/uploads/{a['photo_id']}' alt='Photo of {h(a['stage_name'])}' width='240'></p>")
        body += M(f"<p>{h(a['bio'])}</p><p><strong>Record:</strong> {wins}W {len(results) - wins - draws}L {draws}D</p>")
        if a["consent_media"]:
            body += join(embed(link, f"{a['stage_name']} media") for link in a["media_links"])
        body += M("<h2>Past battles</h2>") + table(["Date", "Event", "Opponent", "Result"], [[fmt_dt(x["decided_at"]), x["event_title"], x["b_name"] if x["a"] == a["id"] else x["a_name"],
                                                                                         "Win" if x["winner"] == a["id"] else "Draw" if x["status"] == "draw" else "Loss"] for x in results], "No battles yet.")
        body += support_block(ctx, a)
        if ctx.user and ctx.user.id == a["user_id"]:
            body += M(f"<p><a class='button' href='/l/{h(slug)}/artists/{a['id']}/edit'>Edit profile</a></p>")
        return ctx.page(a["stage_name"], body, league=slug)

    def support_block(ctx, a):
        if not a["payout_consent"]:
            return M("")
        media = db.all("SELECT * FROM paid_media WHERE artist_id=%s AND active ORDER BY id", (a["id"],))
        out = M("<section class='card'><h2>Support this emcee</h2>")
        uid = ctx.user and ctx.user.id
        if not app.stripe.enabled:
            out += M("<p class='muted'>Tips, memberships and paid media are switched off until the platform enables online payments.</p>")
        elif not ctx.user:
            out += M(f"<p><a href='/login?next=/l/{h(a['league_slug'])}/artists/{a['id']}'>Sign in</a> to tip or join.</p>")
        else:
            out += (M("<form method='post' action='/pay/checkout' class='row'>") + csrf_field(ctx.csrf) + M(f"<input type='hidden' name='kind' value='tip'><input type='hidden' name='artist_id' value='{a['id']}'>")
                    + field("Tip amount (USD)", "amount", value="5", attrs="inputmode='decimal'") + M("<button type='submit'>Tip</button></form>"))
            if payments.has_access(db, uid, kind="membership", artist_id=a["id"]):
                out += M("<p class='pill'>You are a member.</p>")
            else:
                out += post_button("/pay/checkout", f"Become a member — {payments.fmt_money(500)} / 30 days", ctx.csrf, fields={"kind": "membership", "artist_id": a["id"]})
        for m in media:
            unlocked = uid and (uid == a["user_id"] or payments.has_access(db, uid, kind="paid_media", ref_id=m["id"]))
            out += M(f"<p><strong>{h(m['title'])}</strong> — {h(payments.fmt_money(m['price_cents']))} ")
            if unlocked:
                out += M(f"<a href='/l/{h(a['league_slug'])}/media/{m['id']}'>Open</a>")
            elif app.stripe.enabled and ctx.user:
                out += post_button("/pay/checkout", "Buy", ctx.csrf, fields={"kind": "paid_media", "media_id": m["id"]})
            out += M("</p>")
        return out + M("</section>")

    @r.get("/l/{slug}/media/{mid}")
    def paid_media(ctx, slug, mid):
        u = ctx.require()
        m = db.one("SELECT pm.*, a.user_id, a.league_slug FROM paid_media pm JOIN artists a ON a.id=pm.artist_id WHERE pm.id=%s AND a.league_slug=%s", (integer(mid, lo=1), slug))
        if not m or not (u.id == m["user_id"] or payments.has_access(db, u.id, kind="paid_media", ref_id=m["id"])):
            raise HttpError(404, "Not found")
        return ctx.page(m["title"], M(f"<h1>{h(m['title'])}</h1>") + embed(m["url"], m["title"]), league=slug)

    @r.get("/l/{slug}/artists/{aid}/edit")
    def edit_form(ctx, slug, aid):
        a = own(ctx, slug, aid)
        links = list(a["media_links"]) + ["", "", ""]
        events = eng.list_events(slug, statuses=["draft", "scheduled"])
        requests = db.all("SELECT lr.*, e.title FROM lineup_requests lr JOIN events e ON e.id=lr.event_id WHERE lr.artist_id=%s ORDER BY lr.id DESC", (a["id"],))
        media = db.all("SELECT * FROM paid_media WHERE artist_id=%s ORDER BY id", (a["id"],))
        body = (M(f"<h1>Edit {h(a['stage_name'])}</h1><form method='post' action='/l/{h(slug)}/artists/{a['id']}/edit' enctype='multipart/form-data' class='card'>") + csrf_field(ctx.csrf)
                + field("Stage name", "stage_name", value=a["stage_name"], required=True) + field("Hometown", "hometown", value=a["hometown"]) + select("Style", "style", STYLES, a["style"])
                + textarea("Bio", "bio", value=a["bio"]) + field("Media link 1", "link1", type_="url", value=links[0]) + field("Media link 2", "link2", type_="url", value=links[1]) + field("Media link 3", "link3", type_="url", value=links[2])
                + field("Replace photo", "photo", type_="file", attrs="accept='image/jpeg,image/png,image/webp'") + select("Profile visibility", "visibility", VISIBILITY, a["visibility"])
                + check("Show my photo and media links", "consent_media", a["consent_media"])
                + check("Payout consent: I agree to receive my share of tips, memberships, paid media and event pools, and that settlement reports list my stage name and amounts.", "payout_consent", a["payout_consent"],
                        hint="Without payout consent, support buttons are hidden and any event-pool share is held.")
                + M("<button type='submit'>Save profile</button></form>"))
        body += M("<h2>Battle lineups</h2>") + table(["Event", "Status"], [[x["title"], x["status"]] for x in requests], "No lineup requests.")
        if events:
            body += (M(f"<form method='post' action='/l/{h(slug)}/artists/{a['id']}/apply' class='card row'>") + csrf_field(ctx.csrf) + select("Request a spot in", "event_id", [(e["id"], e["title"]) for e in events])
                     + field("Note to the promoter", "note", attrs="maxlength='300'") + M("<button type='submit'>Request spot</button></form>"))
        body += M("<h2>Paid media</h2>") + table(["Title", "Price", "Active"], [[m["title"], payments.fmt_money(m["price_cents"]), "Yes" if m["active"] else "No"] for m in media], "No paid media.")
        if a["payout_consent"]:
            body += (M(f"<form method='post' action='/l/{h(slug)}/artists/{a['id']}/media' class='card'>") + csrf_field(ctx.csrf) + field("Title", "title", required=True)
                     + field("Unlisted media link", "url", type_="url", required=True, hint="Shown only to buyers. YouTube, Vimeo or SoundCloud.") + field("Price (USD)", "price", value="3") + M("<button type='submit'>Add paid media</button></form>"))
        body += M("<section class='card'><h2>Remove profile</h2>") + post_button(f"/l/{slug}/artists/{a['id']}/delete", "Delete this emcee profile", ctx.csrf, cls="danger", confirm="Delete this profile?") + M("</section>")
        return ctx.page("Edit profile", body, league=slug)

    @r.post("/l/{slug}/artists/{aid}/edit", multipart=True)
    def edit(ctx, slug, aid):
        a = own(ctx, slug, aid)
        photo = store_photo(ctx, ctx.req.files.get("photo"))
        payout = checkbox(ctx.form.get("payout_consent"))
        db.run("""UPDATE artists SET stage_name=%s, hometown=%s, style=%s, bio=%s, media_links=%s, visibility=%s, consent_media=%s, photo_id=COALESCE(%s, photo_id),
            payout_consent=%s, payout_consent_at=CASE WHEN %s AND NOT payout_consent THEN now() WHEN NOT %s THEN NULL ELSE payout_consent_at END WHERE id=%s""",
               (text(ctx.form.get("stage_name"), min_len=1, max_len=60, name="Stage name"), text(ctx.form.get("hometown"), max_len=80), one_of(ctx.form.get("style"), [x[0] for x in STYLES], "style"),
                text(ctx.form.get("bio"), max_len=1500, name="Bio"), Jsonb(links_from(ctx.form)), one_of(ctx.form.get("visibility"), [x[0] for x in VISIBILITY], "visibility"),
                checkbox(ctx.form.get("consent_media")), photo, payout, payout, payout, a["id"]))
        if photo and a["photo_id"]:
            db.run("DELETE FROM uploads WHERE id=%s", (a["photo_id"],))
        if not payout:
            db.run("UPDATE paid_media SET active=false WHERE artist_id=%s", (a["id"],))
        audit(db, ctx.user.id, "artist.updated", {"artistId": a["id"], "payoutConsent": payout}, slug)
        return ctx.redirect(f"/l/{slug}/artists/{a['id']}/edit", "Profile saved.")

    @r.post("/l/{slug}/artists/{aid}/apply")
    def apply(ctx, slug, aid):
        a = own(ctx, slug, aid)
        e = eng.event(integer(ctx.form.get("event_id"), lo=1), slug)
        if e["status"] not in ("draft", "scheduled"):
            raise HttpError(409, "That event is not taking lineup requests")
        try:
            db.run("INSERT INTO lineup_requests(event_id,artist_id,note) VALUES (%s,%s,%s)", (e["id"], a["id"], text(ctx.form.get("note"), max_len=300)))
        except UniqueViolation:
            raise HttpError(409, "You already requested a spot in that event")
        return ctx.redirect(f"/l/{slug}/artists/{a['id']}/edit", "Lineup request sent.")

    @r.post("/l/{slug}/artists/{aid}/media")
    def add_media(ctx, slug, aid):
        a = own(ctx, slug, aid)
        if not a["payout_consent"]:
            raise HttpError(409, "Give payout consent before selling media")
        db.run("INSERT INTO paid_media(artist_id,title,url,price_cents) VALUES (%s,%s,%s,%s)",
               (a["id"], text(ctx.form.get("title"), min_len=1, max_len=120, name="Title"), media_link(ctx.form.get("url"), required=True), money(ctx.form.get("price"), name="Price", allow_zero=False)))
        return ctx.redirect(f"/l/{slug}/artists/{a['id']}/edit", "Paid media added.")

    @r.post("/l/{slug}/artists/{aid}/delete")
    def delete(ctx, slug, aid):
        a = own(ctx, slug, aid)
        with db.tx() as q:
            if a["photo_id"]:
                q.run("DELETE FROM uploads WHERE id=%s", (a["photo_id"],))
            q.run("DELETE FROM artists WHERE id=%s", (a["id"],))
        audit(db, ctx.user.id, "artist.deleted", {"artistId": a["id"]}, slug)
        return ctx.redirect("/account", "Emcee profile deleted.")

