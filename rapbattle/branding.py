"""RAP-02 white-label branding: validated per-league tokens rendered without a fork.

Branding is plain data (names, tagline, colours, optional custom domain). It is
rendered through fixed templates and an escaped/allowlisted stylesheet, so a
league cannot inject markup, script, remote assets or CSS outside its tokens.
"""
import re

HEX = re.compile(r"#[0-9a-f]{6}")
DOMAIN = re.compile(r"(?=.{4,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}")
FIELDS = ("display_name", "tagline", "primary_color", "accent_color", "background_color", "text_color")
COLOR_FIELDS = FIELDS[2:]
RESERVED_DOMAIN_SUFFIXES = (".onrender.com", ".neon.tech", ".github.com", ".github.io")


def _luminance(hex_color):
    channels = []
    for i in (1, 3, 5):
        c = int(hex_color[i:i + 2], 16) / 255
        channels.append(c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4)
    return 0.2126 * channels[0] + 0.7152 * channels[1] + 0.0722 * channels[2]


def contrast(a, b):
    x, y = _luminance(a), _luminance(b)
    return (max(x, y) + 0.05) / (min(x, y) + 0.05)


def _text(value, limit, label):
    if not isinstance(value, str) or not 1 <= len(value.strip()) <= limit or any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise ValueError(f"{label} must contain 1 to {limit} printable characters")
    return value.strip()


def validate_branding(value):
    if not isinstance(value, dict) or set(value) != set(FIELDS):
        raise ValueError("Branding requires exactly: " + ", ".join(FIELDS))
    result = {"display_name": _text(value["display_name"], 80, "Display name"),
              "tagline": _text(value["tagline"], 160, "Tagline")}
    for key in COLOR_FIELDS:
        colour = value[key]
        if not isinstance(colour, str) or not HEX.fullmatch(colour.lower()):
            raise ValueError("Colours must be #rrggbb")
        result[key] = colour.lower()
    # WCAG AA for body text and for primary/accent used as text/links on the background.
    for key in ("text_color", "primary_color", "accent_color"):
        if contrast(result[key], result["background_color"]) < 4.5:
            raise ValueError(f"{key} must have at least 4.5:1 contrast with background_color")
    return result


def validate_domain(domain):
    if domain is None:
        return None
    if not isinstance(domain, str):
        raise ValueError("Custom domain must be a hostname or null")
    domain = domain.strip().lower()
    if not DOMAIN.fullmatch(domain) or domain.endswith(RESERVED_DOMAIN_SUFFIXES):
        raise ValueError("Custom domain must be a public hostname without scheme, port or path")
    return domain


def default_branding(name):
    return {"display_name": name[:80], "tagline": "Independent rap battle league",
            "primary_color": "#efc47d", "accent_color": "#9fd3ff",
            "background_color": "#11131a", "text_color": "#f5f2ee"}


def stylesheet(brand):
    """Generated only from validated tokens; served same-origin under CSP style-src 'self'."""
    b = validate_branding({k: brand[k] for k in FIELDS})
    return (":root{color-scheme:dark light;--league-primary:%(primary_color)s;--league-accent:%(accent_color)s;"
            "--league-bg:%(background_color)s;--league-text:%(text_color)s;font-family:system-ui,sans-serif}"
            "*{box-sizing:border-box}body{margin:0;background:var(--league-bg);color:var(--league-text);line-height:1.55}"
            "header,main,footer{max-width:68rem;margin:auto;padding:clamp(1rem,4vw,3rem)}"
            "header{display:flex;flex-wrap:wrap;gap:1rem;justify-content:space-between;align-items:center}"
            ".brand{font-weight:800;font-size:1.4rem;color:var(--league-primary);text-decoration:none}"
            "nav{display:flex;gap:1rem;flex-wrap:wrap}a{color:var(--league-accent);min-height:44px;display:inline-flex;align-items:center}"
            "a:focus-visible{outline:3px solid var(--league-primary);outline-offset:3px}"
            "h1{font-size:clamp(2rem,6vw,3.6rem);line-height:1.1;margin:.5rem 0 1rem;color:var(--league-primary)}"
            ".notice{border:2px dashed var(--league-accent);border-radius:.75rem;padding:1rem}"
            ".grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%%,18rem),1fr));gap:1rem;list-style:none;padding:0}"
            ".card{border:1px solid var(--league-accent);border-radius:1rem;padding:1.25rem}"
            ".skip{position:absolute;left:-9999px}.skip:focus{left:1rem;top:1rem;background:var(--league-bg);padding:1rem}"
            "dl{display:grid;grid-template-columns:max-content 1fr;gap:.25rem 1rem}dd{margin:0}"
            "@media(prefers-reduced-motion:reduce){*{transition:none!important;animation:none!important}}") % b
