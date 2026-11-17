"""HTML layout and components. Every interpolation is escaped through h() unless it is Markup."""
from .http import Markup, h, join

APP_CSS = """:root{color-scheme:dark;--league-primary:#efc47d;--league-accent:#8ec5ff;--league-bg:#11131a;--league-text:#f5f2ee;font-family:system-ui,sans-serif}
*{box-sizing:border-box}body{margin:0;background:var(--league-bg);color:var(--league-text);line-height:1.55}
header,main,footer{max-width:72rem;margin:auto;padding:clamp(1rem,3vw,2.5rem)}header{display:flex;flex-wrap:wrap;gap:1rem;justify-content:space-between;align-items:center}
.brand{font-weight:800;font-size:1.4rem;color:var(--league-primary);text-decoration:none}nav{display:flex;gap:1rem;flex-wrap:wrap;align-items:center}
a{color:var(--league-accent)}a:focus-visible,button:focus-visible,input:focus-visible,select:focus-visible,textarea:focus-visible{outline:3px solid var(--league-primary);outline-offset:3px}
h1{font-size:clamp(2rem,5vw,3.4rem);line-height:1.1;margin:.5rem 0 1rem;color:var(--league-primary)}.eyebrow{text-transform:uppercase;letter-spacing:.1em;font-size:.85rem;font-weight:700;opacity:.85}
.card{border:1px solid color-mix(in srgb,var(--league-accent) 60%,transparent);border-radius:1rem;padding:1.25rem;margin:1rem 0}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%,17rem),1fr));gap:1rem;list-style:none;padding:0}
.notice,.banner{border:2px dashed var(--league-accent);border-radius:.75rem;padding:.75rem 1rem}.flash{border-radius:.75rem;padding:.75rem 1rem;background:#1f4d2b;color:#e6ffe9}.flash.error{background:#5a1717;color:#ffe1e1}
.muted{opacity:.75}.inline{display:inline}.row{display:flex;flex-wrap:wrap;gap:.6rem;align-items:center}
label.field{display:flex;flex-direction:column;gap:.25rem;margin:.6rem 0;max-width:40rem}label.check{display:flex;gap:.5rem;align-items:flex-start;margin:.5rem 0;max-width:44rem}
input,select,textarea{font:inherit;padding:.55rem .65rem;border-radius:.6rem;border:1px solid #6b7385;background:color-mix(in srgb,var(--league-bg) 85%,#fff);color:var(--league-text);min-height:44px}textarea{min-height:5rem}label.check input{min-height:auto;margin-top:.35rem}
button,.button{font:inherit;font-weight:700;border:0;border-radius:.6rem;padding:.55rem 1rem;background:var(--league-primary);color:var(--league-bg);cursor:pointer;text-decoration:none;min-height:44px;display:inline-flex;align-items:center}
button.secondary{background:transparent;color:var(--league-text);border:1px solid var(--league-accent)}button.danger{background:#b3261e;color:#fff}button.link{background:none;border:0;color:var(--league-accent);padding:0 .25rem;font-weight:400}
.table-wrap{overflow-x:auto}table{border-collapse:collapse;width:100%}th,td{text-align:left;padding:.45rem .6rem;border-bottom:1px solid #3c4354;vertical-align:top}
.pill{display:inline-block;padding:.05rem .55rem;border-radius:999px;border:1px solid var(--league-accent);font-size:.85rem}
.timer{font-size:clamp(2.5rem,9vw,5rem);font-weight:800;font-variant-numeric:tabular-nums}.timer.over{color:#ff8a80}
.versus{display:grid;grid-template-columns:1fr auto 1fr;gap:1rem;align-items:center;text-align:center}.versus strong{font-size:1.4rem}
.bracket{display:grid;grid-auto-flow:column;grid-auto-columns:minmax(13rem,1fr);gap:1rem;overflow-x:auto}
iframe.embed{width:100%;max-width:40rem;aspect-ratio:16/9;border:0;border-radius:.75rem}.skip{position:absolute;left:-9999px}.skip:focus{left:1rem;top:1rem;background:var(--league-bg);padding:1rem}
.sponsor{border-left:4px solid var(--league-primary);padding:.5rem 1rem;margin:1rem 0}
@media(prefers-reduced-motion:reduce){*{transition:none!important;animation:none!important}}"""

APP_JS = """(function(){function f(ms){var n=ms<0,s=Math.round(Math.abs(ms)/1000);return(n?'+':'')+Math.floor(s/60)+':'+String(s%60).padStart(2,'0')}
var t=document.querySelectorAll('[data-remaining-ms]'),st=Date.now();function tick(){t.forEach(function(e){var b=Number(e.getAttribute('data-remaining-ms')),r=e.getAttribute('data-running')==='1',ms=r?b-(Date.now()-st):b;e.textContent=f(ms);e.classList.toggle('over',ms<0)})}
if(t.length){tick();setInterval(tick,250)}var r=document.querySelector('[data-refresh]');if(r)setTimeout(function(){location.reload()},Number(r.getAttribute('data-refresh'))*1000);
document.addEventListener('submit',function(e){var b=e.target.querySelector('[data-confirm]');if(b&&!confirm(b.getAttribute('data-confirm')))e.preventDefault()})})();"""

FRAME_SOURCES = "https://www.youtube-nocookie.com https://player.vimeo.com https://w.soundcloud.com"


def M(s):
    return Markup(s)


def csrf_field(csrf):
    return M(f"<input type='hidden' name='_csrf' value='{h(csrf)}'>")


def post_button(action, label, csrf, *, fields=None, cls="", confirm=None):
    hidden = join(M(f"<input type='hidden' name='{h(k)}' value='{h(v)}'>") for k, v in (fields or {}).items())
    conf = M(f" data-confirm='{h(confirm)}'") if confirm else ""
    return M(f"<form method='post' action='{h(action)}' class='inline'>{csrf_field(csrf)}{hidden}<button type='submit' class='{h(cls)}'{conf}>{h(label)}</button></form>")


def field(label, name, *, value="", type_="text", required=False, hint="", attrs=""):
    req = " required" if required else ""
    return M(f"<label class='field'><span>{h(label)}{' *' if required else ''}</span><input type='{h(type_)}' name='{h(name)}' value='{h(value if value is not None else '')}'{req} {attrs}>"
             + (f"<small class='muted'>{h(hint)}</small>" if hint else "") + "</label>")


def textarea(label, name, *, value="", required=False, hint="", rows=4):
    return M(f"<label class='field'><span>{h(label)}{' *' if required else ''}</span><textarea name='{h(name)}' rows='{rows}'{' required' if required else ''}>{h(value or '')}</textarea>"
             + (f"<small class='muted'>{h(hint)}</small>" if hint else "") + "</label>")


def select(label, name, options, current=""):
    opts = join(M(f"<option value='{h(v)}'{' selected' if str(v) == str(current) else ''}>{h(l)}</option>") for v, l in options)
    return M(f"<label class='field'><span>{h(label)}</span><select name='{h(name)}'>{opts}</select></label>")


def check(label, name, checked=False, *, hint="", required=False):
    return M(f"<label class='check'><input type='checkbox' name='{h(name)}'{' checked' if checked else ''}{' required' if required else ''}><span>{h(label)}"
             + (f"<br><small class='muted'>{h(hint)}</small>" if hint else "") + "</span></label>")


def table(headers, rows, empty="Nothing here yet."):
    if not rows:
        return M(f"<p class='muted'>{h(empty)}</p>")
    head = join(M(f"<th scope='col'>{h(x)}</th>") for x in headers)
    body = join(M("<tr>" + "".join(f"<td>{h(c)}</td>" for c in r) + "</tr>") for r in rows)
    return M(f"<div class='table-wrap'><table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table></div>")


def fmt_dt(d):
    if not d:
        return ""
    if isinstance(d, str):
        return d
    return d.strftime("%Y-%m-%d %H:%M UTC")


def fmt_ms(ms):
    s = max(0, round((ms or 0) / 1000))
    return f"{s // 60}:{s % 60:02d}"


def layout(*, title, body, user=None, csrf="", flash=None, league=None, brand=None, notice=None, refresh=None):
    """league: slug for branded pages (loads that league's brand.css after the app stylesheet)."""
    css = M("<link rel='stylesheet' href='/assets/app.css'>") + (M(f"<link rel='stylesheet' href='/l/{h(league)}/brand.css'>") if league else "")
    if league:
        name = (brand or {}).get("display_name") or league
        nav = [(f"/l/{league}", "Home"), (f"/l/{league}/events", "Battles"), (f"/l/{league}/artists", "Emcees"), (f"/l/{league}/standings", "Standings"), (f"/l/{league}/formats", "Formats")]
        home = f"/l/{league}"
    else:
        name, home = "Rap Battle", "/"
        nav = [("/leagues", "Leagues"), ("/privacy", "Privacy")]
    links = [("/judge", "Judge")] if user and (user.is_admin or any("judge" in r for r in user.leagues.values())) else []
    if user and (user.is_admin or any("producer" in r for r in user.leagues.values())):
        links.append(("/manage", "Manage"))
    if user and user.is_admin:
        links.append(("/admin", "Admin"))
    acct = (join(M(f"<a href='{h(u)}'>{h(l)}</a>") for u, l in links) + M(f"<a href='/account'>{h(user.name)}</a><form method='post' action='/logout' class='inline'>{csrf_field(csrf)}<button class='link' type='submit'>Sign out</button></form>")
            if user else M("<a href='/login'>Sign in</a><a href='/register'>Create account</a>"))
    flash_html = M(f"<p class='flash {h(flash.get('kind', ''))}' role='{'alert' if flash.get('kind') == 'error' else 'status'}'>{h(flash.get('text'))}</p>") if flash else ""
    notice_html = M(f"<p class='notice' role='note'>{h(notice)}</p>") if notice else ""
    refresh_attr = f" data-refresh='{int(refresh)}'" if refresh else ""
    return ("<!doctype html><html lang='en'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>"
            f"<title>{h(title)} | {h(name)}</title>{css}<script src='/assets/app.js' defer></script></head><body{refresh_attr}>"
            "<a class='skip' href='#main'>Skip to content</a>"
            f"<header><a class='brand' href='{h(home)}'>{h(name)}</a><nav aria-label='Primary'>{join(M(f'<a href={chr(39)}{h(u)}{chr(39)}>{h(l)}</a>') for u, l in nav)}</nav>"
            f"<nav aria-label='Account'>{acct}</nav></header><main id='main' tabindex='-1'>{notice_html}{flash_html}{body}</main>"
            f"<footer class='muted'><p>{'<a href=' + chr(39) + '/' + chr(39) + '>Rap Battle platform</a> · ' if league else ''}<a href='/leagues'>Find a league</a> · <a href='/privacy'>Privacy</a></p></footer></body></html>")


def embed(link, title):
    from .media import embed_url
    src = embed_url(link)
    if not src:
        return M(f"<p><a href='{h(link)}' rel='noopener noreferrer'>Open media</a></p>") if link else M("")
    return M(f"<iframe class='embed' src='{h(src)}' title='{h(title)}' loading='lazy' allow='fullscreen; picture-in-picture' referrerpolicy='strict-origin-when-cross-origin' sandbox='allow-scripts allow-same-origin allow-presentation'></iframe>")

