"""Media as links (YouTube, Vimeo, SoundCloud) plus small image uploads stored in Postgres."""
import re
from urllib.parse import urlsplit, parse_qs, quote

from .http import HttpError

HOSTS = {
    "youtube": ("youtube.com", "www.youtube.com", "m.youtube.com", "youtu.be"),
    "vimeo": ("vimeo.com", "www.vimeo.com", "player.vimeo.com"),
    "soundcloud": ("soundcloud.com", "www.soundcloud.com", "m.soundcloud.com"),
}
ALL_HOSTS = {x for v in HOSTS.values() for x in v}


def media_link(value, *, required=False, name="Media link"):
    s = str(value or "").strip()
    if not s:
        if required:
            raise HttpError(400, f"{name} is required")
        return None
    u = urlsplit(s)
    if u.scheme != "https" or u.username or u.password or len(s) > 500 or (u.hostname or "").lower() not in ALL_HOSTS:
        raise HttpError(400, f"{name} must be an https YouTube, Vimeo or SoundCloud link")
    return s


def embed_url(link):
    if not link:
        return None
    u = urlsplit(link)
    host = (u.hostname or "").lower()
    if host in HOSTS["youtube"]:
        vid = u.path[1:] if host == "youtu.be" else (parse_qs(u.query).get("v", [None])[0] or (re.match(r"^/(?:embed|shorts|live)/([\w-]{6,20})", u.path) or [None, None])[1])
        return f"https://www.youtube-nocookie.com/embed/{vid}" if vid and re.fullmatch(r"[\w-]{6,20}", vid) else None
    if host in HOSTS["vimeo"]:
        m = re.search(r"(\d{5,12})", u.path)
        return f"https://player.vimeo.com/video/{m.group(1)}" if m else None
    if host in HOSTS["soundcloud"]:
        return "https://w.soundcloud.com/player/?url=" + quote(link, safe="")
    return None


def sniff_image(data):
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def validate_image(file, max_bytes):
    if not file:
        return None
    data = file["data"]
    if len(data) > max_bytes:
        raise HttpError(413, f"Photo exceeds the {max_bytes // 1024} KB upload limit; use a smaller image")
    mime = sniff_image(data)
    if not mime:
        raise HttpError(400, "Photo must be a JPEG, PNG or WebP image")
    return {"mime": mime, "data": data}
