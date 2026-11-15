"""Downstream league settings, branding and preset configuration.

Never stores engine tokens, role grants, artist personal data or payment data.
Two interchangeable backends share one portable SQL schema:

* ``Store(path)``: file-backed SQLite (local development and tests).
* ``PostgresStore(dsn)``: Postgres/Neon (deployment; Render free has no disk).

Every tenant-scoped query includes the league slug (and, for writes, the
engine organization and expected version) so a caller can never move or read
another tenant's records by changing a body field.
"""
import json
import re
import sqlite3
from contextlib import contextmanager

from . import branding as brand_rules
from . import presets as preset_rules


SLUG = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*", re.ASCII)
SCHEMA_VERSION = 2


class Conflict(Exception):
    pass


TABLES = [
    """CREATE TABLE IF NOT EXISTS leagues (
        slug TEXT PRIMARY KEY,
        organization_id TEXT UNIQUE NOT NULL,
        name TEXT NOT NULL,
        timezone TEXT NOT NULL,
        version INTEGER NOT NULL DEFAULT 1 CHECK(version>0),
        synthetic INTEGER NOT NULL DEFAULT 0 CHECK(synthetic IN (0,1))
    )""",
    """CREATE TABLE IF NOT EXISTS league_branding (
        slug TEXT PRIMARY KEY REFERENCES leagues(slug),
        branding TEXT NOT NULL,
        custom_domain TEXT UNIQUE,
        version INTEGER NOT NULL DEFAULT 1 CHECK(version>0)
    )""",
    """CREATE TABLE IF NOT EXISTS league_presets (
        slug TEXT NOT NULL REFERENCES leagues(slug),
        preset_id TEXT NOT NULL,
        config TEXT NOT NULL,
        version INTEGER NOT NULL DEFAULT 1 CHECK(version>0),
        PRIMARY KEY (slug, preset_id)
    )""",
]


class _Base:
    placeholder = "?"

    def q(self, sql):
        return sql if self.placeholder == "?" else sql.replace("?", "%s")

    def _one(self, db, sql, params=()):
        row = db.execute(self.q(sql), params).fetchone()
        return dict(row) if row else None

    def _all(self, db, sql, params=()):
        return [dict(r) for r in db.execute(self.q(sql), params).fetchall()]

    # ---- leagues (RAP-01) -------------------------------------------------
    def add_league(self, slug, organization_id, name, timezone="UTC", *, synthetic=False):
        """Operator provisioning, never a public HTTP action or membership grant."""
        if not isinstance(slug, str) or len(slug) > 63 or not SLUG.fullmatch(slug):
            raise ValueError("Invalid league slug")
        if not isinstance(organization_id, str) or not organization_id.strip() or len(organization_id) > 128:
            raise ValueError("Invalid engine organization")
        name, timezone = validate_settings(name, timezone)
        with self.connection() as db:
            db.execute(self.q("INSERT INTO leagues(slug,organization_id,name,timezone,synthetic) VALUES(?,?,?,?,?)"),
                       (slug, organization_id, name, timezone, int(synthetic)))
            default_brand = brand_rules.default_branding(name)
            db.execute(self.q("INSERT INTO league_branding(slug,branding,custom_domain,version) VALUES(?,?,?,1)"),
                       (slug, json.dumps(default_brand, sort_keys=True), None))

    def league(self, slug):
        if not isinstance(slug, str):
            return None
        with self.connection() as db:
            row = self._one(db, "SELECT * FROM leagues WHERE slug=?", (slug,))
        if row:
            row["synthetic"] = bool(row["synthetic"])
        return row

    def update_settings(self, slug, organization_id, name, timezone, version):
        name, timezone = validate_settings(name, timezone)
        if type(version) is not int or version < 1:
            raise ValueError("A positive settings version is required")
        with self.connection() as db:
            cursor = db.execute(self.q("UPDATE leagues SET name=?,timezone=?,version=version+1 "
                                       "WHERE slug=? AND organization_id=? AND version=?"),
                                (name, timezone, slug, organization_id, version))
            if cursor.rowcount != 1:
                raise Conflict("Settings changed; reload before retrying")
        return self.league(slug)

    # ---- branding (RAP-02) ------------------------------------------------
    def branding(self, slug):
        with self.connection() as db:
            row = self._one(db, "SELECT branding,custom_domain,version FROM league_branding WHERE slug=?", (slug,))
        if not row:
            return None
        value = brand_rules.validate_branding(json.loads(row["branding"]))
        return {**value, "custom_domain": row["custom_domain"], "version": row["version"]}

    def update_branding(self, slug, organization_id, branding, custom_domain, version):
        value = brand_rules.validate_branding(branding)
        domain = brand_rules.validate_domain(custom_domain)
        if type(version) is not int or version < 1:
            raise ValueError("A positive branding version is required")
        try:
            with self.connection() as db:
                # The organization check is in the same statement as the write.
                cursor = db.execute(self.q(
                    "UPDATE league_branding SET branding=?,custom_domain=?,version=version+1 "
                    "WHERE slug=? AND version=? AND slug IN (SELECT slug FROM leagues WHERE slug=? AND organization_id=?)"),
                    (json.dumps(value, sort_keys=True), domain, slug, version, slug, organization_id))
                if cursor.rowcount != 1:
                    raise Conflict("Branding changed; reload before retrying")
        except Exception as error:
            if any(t.__name__ == "IntegrityError" for t in type(error).__mro__):
                raise Conflict("Custom domain is already mapped to another league") from None
            raise
        return self.branding(slug)

    def league_by_domain(self, host):
        domain = (host or "").lower()
        if not brand_rules.DOMAIN.fullmatch(domain):
            return None
        with self.connection() as db:
            row = self._one(db, "SELECT slug FROM league_branding WHERE custom_domain=?", (domain,))
        return row["slug"] if row else None

    # ---- presets (RAP-04) -------------------------------------------------
    def presets(self, slug):
        with self.connection() as db:
            rows = self._all(db, "SELECT preset_id,config,version FROM league_presets WHERE slug=?", (slug,))
        stored = {r["preset_id"]: (json.loads(r["config"]), r["version"]) for r in rows}
        result = []
        for preset_id in preset_rules.PRESET_IDS:
            config, version = stored.get(preset_id, (preset_rules.default_config(preset_id), 0))
            result.append({"id": preset_id, "version": version,
                           "config": preset_rules.validate_config(preset_id, config)})
        return result

    def update_preset(self, slug, organization_id, preset_id, config, version):
        checked = preset_rules.validate_config(preset_id, config)
        if type(version) is not int or version < 0:
            raise ValueError("A non-negative preset version is required")
        payload = json.dumps(checked, sort_keys=True)
        with self.connection() as db:
            owner = self._one(db, "SELECT slug FROM leagues WHERE slug=? AND organization_id=?", (slug, organization_id))
            if not owner:
                raise Conflict("League changed; reload before retrying")
            if version == 0:
                cursor = db.execute(self.q("INSERT INTO league_presets(slug,preset_id,config,version) VALUES(?,?,?,1) "
                                           "ON CONFLICT (slug,preset_id) DO NOTHING"), (slug, preset_id, payload))
            else:
                cursor = db.execute(self.q("UPDATE league_presets SET config=?,version=version+1 "
                                           "WHERE slug=? AND preset_id=? AND version=?"),
                                    (payload, slug, preset_id, version))
            if cursor.rowcount != 1:
                raise Conflict("Preset changed; reload before retrying")
        return next(p for p in self.presets(slug) if p["id"] == preset_id)


class Store(_Base):
    """SQLite backend (local development, tests)."""

    def __init__(self, path):
        self.path = str(path)
        if self.path == ":memory:":
            raise ValueError("Use a file-backed database for persistent settings")
        with self.connection() as db:
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0, 1, SCHEMA_VERSION):
                raise ValueError("Unsupported downstream schema version")
            db.execute("PRAGMA journal_mode=WAL")
            for table in TABLES:
                db.execute(table)
            # v1 -> v2: leagues created before branding existed get defaults.
            db.execute("INSERT INTO league_branding(slug,branding,custom_domain,version) "
                       "SELECT slug, '{}', NULL, 1 FROM leagues WHERE slug NOT IN (SELECT slug FROM league_branding)")
            for row in db.execute("SELECT l.slug,l.name FROM leagues l JOIN league_branding b ON b.slug=l.slug "
                                  "WHERE b.branding='{}'").fetchall():
                db.execute("UPDATE league_branding SET branding=? WHERE slug=?",
                           (json.dumps(brand_rules.default_branding(row[1]), sort_keys=True), row[0]))
            db.execute(f"PRAGMA user_version={SCHEMA_VERSION}")

    @contextmanager
    def connection(self):
        db = sqlite3.connect(self.path, timeout=5)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        try:
            with db:
                yield db
        finally:
            db.close()

    def healthy(self):
        try:
            with self.connection() as db:
                return db.execute("PRAGMA quick_check").fetchone()[0] == "ok"
        except sqlite3.Error:
            return False


class PostgresStore(_Base):
    """Postgres backend (Neon). Same schema and semantics as the SQLite store."""
    placeholder = "%s"

    def __init__(self, dsn, *, pool_size=4):
        from psycopg.rows import dict_row
        from psycopg_pool import ConnectionPool
        self.pool = ConnectionPool(dsn, min_size=1, max_size=pool_size, open=True, timeout=10,
                                   kwargs={"row_factory": dict_row, "connect_timeout": 10})
        with self.connection() as db:
            db.execute("SELECT pg_advisory_xact_lock(724001)")
            db.execute("CREATE TABLE IF NOT EXISTS rap_schema_version (version INTEGER PRIMARY KEY)")
            current = db.execute("SELECT max(version) AS v FROM rap_schema_version").fetchone()["v"]
            if current not in (None, SCHEMA_VERSION):
                raise ValueError("Unsupported downstream schema version")
            for table in TABLES:
                db.execute(table)
            db.execute("INSERT INTO rap_schema_version(version) VALUES(%s) ON CONFLICT DO NOTHING", (SCHEMA_VERSION,))

    @contextmanager
    def connection(self):
        with self.pool.connection() as db:
            with db.transaction():
                yield db

    def healthy(self):
        try:
            with self.connection() as db:
                return db.execute("SELECT 1 AS ok").fetchone()["ok"] == 1
        except Exception:
            return False

    def close(self):
        self.pool.close()


def validate_settings(name, timezone):
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
    if not isinstance(name, str) or not 1 <= len(name.strip()) <= 120 or any(ord(c) < 32 for c in name):
        raise ValueError("League name must contain 1 to 120 printable characters")
    if not isinstance(timezone, str) or len(timezone) > 64:
        raise ValueError("A valid IANA timezone is required")
    try:
        ZoneInfo(timezone)
    except (ZoneInfoNotFoundError, ValueError):
        raise ValueError("A valid IANA timezone is required") from None
    return name.strip(), timezone
