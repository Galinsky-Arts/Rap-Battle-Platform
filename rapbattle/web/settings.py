"""Platform settings. Artist sign-up stays CLOSED until an admin records the privacy policy details
(operator, privacy contact, retention days, policy version, policy text) and switches it on."""
import time

from psycopg.types.json import Jsonb

from .http import HttpError

DEFAULTS = {
    "operator_name": "", "privacy_contact": "", "retention_days": 0, "policy_version": "", "policy_text": "",
    "artist_signup_open": False, "vote_min_account_minutes": 10, "vote_ip_cap": 25, "upload_max_kb": 1024,
}


def privacy_gaps(s):
    gaps = []
    if not str(s.get("operator_name") or "").strip():
        gaps.append("operator name")
    if not str(s.get("privacy_contact") or "").strip():
        gaps.append("privacy contact")
    if not (isinstance(s.get("retention_days"), int) and s["retention_days"] >= 1):
        gaps.append("retention days")
    if not str(s.get("policy_version") or "").strip():
        gaps.append("policy version")
    if not str(s.get("policy_text") or "").strip():
        gaps.append("privacy policy text")
    return gaps


class Settings:
    def __init__(self, db):
        self.db, self._cache, self._at = db, None, 0

    def all(self):
        if self._cache and time.time() - self._at < 2:
            return dict(self._cache)
        s = dict(DEFAULTS)
        for r in self.db.all("SELECT key, value FROM platform_settings"):
            if r["key"] in DEFAULTS:
                s[r["key"]] = r["value"]
        if privacy_gaps(s):
            s["artist_signup_open"] = False
        self._cache, self._at = s, time.time()
        return dict(s)

    def _put(self, key, value):
        self.db.run("""INSERT INTO platform_settings(key,value) VALUES (%s,%s)
            ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value, updated_at=now()""", (key, Jsonb(value)))
        self._cache = None

    def update(self, values):
        for k, v in values.items():
            if k not in DEFAULTS:
                raise HttpError(400, "Unknown setting")
            self._put(k, v)
        return self.all()

    def set_signup_open(self, open_):
        if open_:
            gaps = privacy_gaps(self.all())
            if gaps:
                raise HttpError(409, "Artist sign-up cannot open until these are set in Settings: " + ", ".join(gaps) + ".")
        self._put("artist_signup_open", bool(open_))
        return self.all()
