"""Postgres access for the full application (psycopg 3 pool) and ordered migrations."""
from contextlib import contextmanager

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

MIGRATIONS = [
    ("001_accounts", """
CREATE TABLE IF NOT EXISTS users (
 id BIGSERIAL PRIMARY KEY, email TEXT NOT NULL UNIQUE, password_hash TEXT NOT NULL, display_name TEXT NOT NULL,
 created_at TIMESTAMPTZ NOT NULL DEFAULT now(), disabled BOOLEAN NOT NULL DEFAULT false, bootstrap BOOLEAN NOT NULL DEFAULT false);
CREATE TABLE IF NOT EXISTS user_roles (
 user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE, role TEXT NOT NULL CHECK (role IN ('admin','fan')), PRIMARY KEY (user_id, role));
CREATE TABLE IF NOT EXISTS league_members (
 league_slug TEXT NOT NULL REFERENCES leagues(slug) ON DELETE CASCADE, user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
 role TEXT NOT NULL CHECK (role IN ('producer','judge')), PRIMARY KEY (league_slug, user_id, role));
CREATE TABLE IF NOT EXISTS sessions (
 id TEXT PRIMARY KEY, user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE, csrf TEXT NOT NULL,
 created_at TIMESTAMPTZ NOT NULL DEFAULT now(), expires_at TIMESTAMPTZ NOT NULL);
CREATE TABLE IF NOT EXISTS platform_settings (key TEXT PRIMARY KEY, value JSONB NOT NULL, updated_at TIMESTAMPTZ NOT NULL DEFAULT now());
CREATE TABLE IF NOT EXISTS league_settings (
 league_slug TEXT PRIMARY KEY REFERENCES leagues(slug) ON DELETE CASCADE, city TEXT NOT NULL DEFAULT '', region TEXT NOT NULL DEFAULT '',
 description TEXT NOT NULL DEFAULT '', listed BOOLEAN NOT NULL DEFAULT true, sponsorship_enabled BOOLEAN NOT NULL DEFAULT false,
 contact_email TEXT NOT NULL DEFAULT '');
CREATE TABLE IF NOT EXISTS audit_log (
 id BIGSERIAL PRIMARY KEY, at TIMESTAMPTZ NOT NULL DEFAULT now(), actor_user_id BIGINT, league_slug TEXT, action TEXT NOT NULL, detail JSONB NOT NULL DEFAULT '{}');
CREATE TABLE IF NOT EXISTS notifications (
 id BIGSERIAL PRIMARY KEY, user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE, at TIMESTAMPTZ NOT NULL DEFAULT now(),
 message TEXT NOT NULL, read BOOLEAN NOT NULL DEFAULT false);
"""),
    ("002_artists", """
CREATE TABLE IF NOT EXISTS artists (
 id BIGSERIAL PRIMARY KEY, league_slug TEXT NOT NULL REFERENCES leagues(slug) ON DELETE CASCADE,
 user_id BIGINT REFERENCES users(id) ON DELETE CASCADE, stage_name TEXT NOT NULL, bio TEXT NOT NULL DEFAULT '',
 hometown TEXT NOT NULL DEFAULT '', style TEXT NOT NULL DEFAULT 'both' CHECK (style IN ('written','freestyle','both')),
 media_links JSONB NOT NULL DEFAULT '[]', photo_id BIGINT,
 visibility TEXT NOT NULL DEFAULT 'public' CHECK (visibility IN ('public','promoters','private')),
 status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','hidden')), moderation_note TEXT NOT NULL DEFAULT '',
 consent_version TEXT NOT NULL, consent_profile BOOLEAN NOT NULL, consent_media BOOLEAN NOT NULL DEFAULT false,
 consent_at TIMESTAMPTZ NOT NULL DEFAULT now(), consent_ip_hash TEXT NOT NULL DEFAULT '',
 payout_consent BOOLEAN NOT NULL DEFAULT false, payout_consent_at TIMESTAMPTZ,
 created_at TIMESTAMPTZ NOT NULL DEFAULT now(), UNIQUE (league_slug, user_id));
CREATE TABLE IF NOT EXISTS uploads (
 id BIGSERIAL PRIMARY KEY, owner_user_id BIGINT REFERENCES users(id) ON DELETE CASCADE, mime TEXT NOT NULL, size INT NOT NULL,
 data BYTEA NOT NULL, created_at TIMESTAMPTZ NOT NULL DEFAULT now());
CREATE TABLE IF NOT EXISTS contracts (
 id BIGSERIAL PRIMARY KEY, league_slug TEXT NOT NULL REFERENCES leagues(slug) ON DELETE CASCADE, artist_id BIGINT NOT NULL REFERENCES artists(id) ON DELETE CASCADE,
 event_id BIGINT, title TEXT NOT NULL, terms TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'sent' CHECK (status IN ('sent','accepted','declined','void')),
 created_at TIMESTAMPTZ NOT NULL DEFAULT now(), decided_at TIMESTAMPTZ);
"""),
    ("003_engine", """
CREATE TABLE IF NOT EXISTS seasons (
 id BIGSERIAL PRIMARY KEY, league_slug TEXT NOT NULL REFERENCES leagues(slug) ON DELETE CASCADE, name TEXT NOT NULL,
 status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','closed')), points_win INT NOT NULL DEFAULT 3,
 points_draw INT NOT NULL DEFAULT 1, points_loss INT NOT NULL DEFAULT 0, created_at TIMESTAMPTZ NOT NULL DEFAULT now());
CREATE TABLE IF NOT EXISTS events (
 id BIGSERIAL PRIMARY KEY, league_slug TEXT NOT NULL REFERENCES leagues(slug) ON DELETE CASCADE, season_id BIGINT REFERENCES seasons(id) ON DELETE SET NULL,
 slug TEXT NOT NULL, title TEXT NOT NULL, description TEXT NOT NULL DEFAULT '', venue TEXT NOT NULL DEFAULT '', starts_at TIMESTAMPTZ,
 status TEXT NOT NULL DEFAULT 'draft' CHECK (status IN ('draft','scheduled','live','completed','cancelled')), published BOOLEAN NOT NULL DEFAULT false,
 preset_id TEXT NOT NULL, config JSONB NOT NULL, voting_enabled BOOLEAN NOT NULL DEFAULT false, votes_per_fan INT NOT NULL DEFAULT 1,
 stream_url TEXT, ticket_cents INT NOT NULL DEFAULT 0, ppv_cents INT NOT NULL DEFAULT 0, champion_entrant_id BIGINT,
 synthetic BOOLEAN NOT NULL DEFAULT false, created_at TIMESTAMPTZ NOT NULL DEFAULT now(), finalized_at TIMESTAMPTZ, UNIQUE (league_slug, slug));
CREATE TABLE IF NOT EXISTS criteria (
 id BIGSERIAL PRIMARY KEY, event_id BIGINT NOT NULL REFERENCES events(id) ON DELETE CASCADE, name TEXT NOT NULL,
 weight_bps INT NOT NULL CHECK (weight_bps BETWEEN 1 AND 10000), max_score INT NOT NULL DEFAULT 10, position INT NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS entrants (
 id BIGSERIAL PRIMARY KEY, event_id BIGINT NOT NULL REFERENCES events(id) ON DELETE CASCADE, artist_id BIGINT REFERENCES artists(id) ON DELETE SET NULL,
 display_name TEXT NOT NULL, seed INT, status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','withdrawn','disqualified')), UNIQUE (event_id, artist_id));
CREATE TABLE IF NOT EXISTS judges (event_id BIGINT NOT NULL REFERENCES events(id) ON DELETE CASCADE, user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE, PRIMARY KEY (event_id, user_id));
CREATE TABLE IF NOT EXISTS lineup_requests (
 id BIGSERIAL PRIMARY KEY, event_id BIGINT NOT NULL REFERENCES events(id) ON DELETE CASCADE, artist_id BIGINT NOT NULL REFERENCES artists(id) ON DELETE CASCADE,
 note TEXT NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','accepted','declined')), created_at TIMESTAMPTZ NOT NULL DEFAULT now(), UNIQUE (event_id, artist_id));
CREATE TABLE IF NOT EXISTS matches (
 id BIGSERIAL PRIMARY KEY, event_id BIGINT NOT NULL REFERENCES events(id) ON DELETE CASCADE, stage INT NOT NULL DEFAULT 1, position INT NOT NULL DEFAULT 1,
 side_a BIGINT REFERENCES entrants(id), side_b BIGINT REFERENCES entrants(id),
 status TEXT NOT NULL DEFAULT 'scheduled' CHECK (status IN ('scheduled','in_progress','judging','overtime','decided','draw','forfeited','cancelled')),
 current_round INT NOT NULL DEFAULT 0, overtimes INT NOT NULL DEFAULT 0, winner BIGINT REFERENCES entrants(id), outcome_reason TEXT NOT NULL DEFAULT '',
 timer_side TEXT, timer_started_at TIMESTAMPTZ, timer_elapsed_ms BIGINT NOT NULL DEFAULT 0, timer_running BOOLEAN NOT NULL DEFAULT false,
 voting_open BOOLEAN NOT NULL DEFAULT false, next_match_id BIGINT REFERENCES matches(id), next_slot TEXT CHECK (next_slot IN ('a','b')),
 recording_url TEXT, decided_at TIMESTAMPTZ, UNIQUE (event_id, stage, position));
CREATE TABLE IF NOT EXISTS turns (
 match_id BIGINT NOT NULL REFERENCES matches(id) ON DELETE CASCADE, round INT NOT NULL, side TEXT NOT NULL CHECK (side IN ('a','b')),
 elapsed_ms BIGINT NOT NULL, over_ms BIGINT NOT NULL DEFAULT 0, PRIMARY KEY (match_id, round, side));
CREATE TABLE IF NOT EXISTS scores (
 match_id BIGINT NOT NULL REFERENCES matches(id) ON DELETE CASCADE, judge_user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
 round INT NOT NULL, side TEXT NOT NULL CHECK (side IN ('a','b')), criterion_id BIGINT NOT NULL REFERENCES criteria(id) ON DELETE CASCADE,
 value NUMERIC(6,2) NOT NULL CHECK (value >= 0), PRIMARY KEY (match_id, judge_user_id, round, side, criterion_id));
CREATE TABLE IF NOT EXISTS votes (
 id BIGSERIAL PRIMARY KEY, match_id BIGINT NOT NULL REFERENCES matches(id) ON DELETE CASCADE, side TEXT NOT NULL CHECK (side IN ('a','b')),
 user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE, ip_hash TEXT NOT NULL, created_at TIMESTAMPTZ NOT NULL DEFAULT now(), UNIQUE (match_id, user_id));
CREATE INDEX IF NOT EXISTS votes_ip ON votes(match_id, ip_hash);
CREATE TABLE IF NOT EXISTS disputes (
 id BIGSERIAL PRIMARY KEY, match_id BIGINT NOT NULL REFERENCES matches(id) ON DELETE CASCADE, raised_by BIGINT REFERENCES users(id) ON DELETE SET NULL,
 reason TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open','upheld','rejected')), resolution TEXT NOT NULL DEFAULT '',
 created_at TIMESTAMPTZ NOT NULL DEFAULT now(), resolved_at TIMESTAMPTZ);
"""),
    ("004_money", """
CREATE TABLE IF NOT EXISTS split_rules (
 league_slug TEXT NOT NULL REFERENCES leagues(slug) ON DELETE CASCADE, kind TEXT NOT NULL,
 platform_bps INT NOT NULL, league_bps INT NOT NULL, artist_bps INT NOT NULL, event_pool_bps INT NOT NULL,
 CHECK (platform_bps + league_bps + artist_bps + event_pool_bps = 10000), PRIMARY KEY (league_slug, kind));
CREATE TABLE IF NOT EXISTS paid_media (
 id BIGSERIAL PRIMARY KEY, artist_id BIGINT NOT NULL REFERENCES artists(id) ON DELETE CASCADE, title TEXT NOT NULL, url TEXT NOT NULL,
 price_cents INT NOT NULL CHECK (price_cents >= 50), active BOOLEAN NOT NULL DEFAULT true, created_at TIMESTAMPTZ NOT NULL DEFAULT now());
CREATE TABLE IF NOT EXISTS orders (
 id BIGSERIAL PRIMARY KEY, league_slug TEXT NOT NULL REFERENCES leagues(slug), kind TEXT NOT NULL CHECK (kind IN ('ticket','ppv','tip','membership','paid_media','sponsor')),
 buyer_user_id BIGINT REFERENCES users(id) ON DELETE SET NULL, artist_id BIGINT REFERENCES artists(id) ON DELETE SET NULL, event_id BIGINT REFERENCES events(id) ON DELETE SET NULL,
 ref_id BIGINT, amount_cents INT NOT NULL CHECK (amount_cents > 0), currency TEXT NOT NULL DEFAULT 'usd',
 status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','paid','refunded','partially_refunded','disputed','expired')),
 refunded_cents INT NOT NULL DEFAULT 0, stripe_session_id TEXT UNIQUE, stripe_payment_intent TEXT, created_at TIMESTAMPTZ NOT NULL DEFAULT now(), paid_at TIMESTAMPTZ,
 access_until TIMESTAMPTZ);
CREATE TABLE IF NOT EXISTS ledger (
 id BIGSERIAL PRIMARY KEY, order_id BIGINT NOT NULL REFERENCES orders(id), league_slug TEXT NOT NULL, party TEXT NOT NULL CHECK (party IN ('platform','league','artist','event_pool')),
 artist_id BIGINT, event_id BIGINT, amount_cents INT NOT NULL, entry TEXT NOT NULL CHECK (entry IN ('sale','refund','chargeback')),
 settlement_id BIGINT, created_at TIMESTAMPTZ NOT NULL DEFAULT now());
CREATE TABLE IF NOT EXISTS settlements (
 id BIGSERIAL PRIMARY KEY, league_slug TEXT NOT NULL REFERENCES leagues(slug), created_by BIGINT, created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
 totals JSONB NOT NULL, evidence_sha256 TEXT NOT NULL, entry_count INT NOT NULL);
CREATE TABLE IF NOT EXISTS stripe_events (id TEXT PRIMARY KEY, type TEXT NOT NULL, received_at TIMESTAMPTZ NOT NULL DEFAULT now());
CREATE TABLE IF NOT EXISTS sponsors (
 id BIGSERIAL PRIMARY KEY, league_slug TEXT NOT NULL REFERENCES leagues(slug) ON DELETE CASCADE, name TEXT NOT NULL, url TEXT NOT NULL DEFAULT '',
 contact_email TEXT NOT NULL DEFAULT '', created_at TIMESTAMPTZ NOT NULL DEFAULT now());
CREATE TABLE IF NOT EXISTS placements (
 id BIGSERIAL PRIMARY KEY, league_slug TEXT NOT NULL REFERENCES leagues(slug) ON DELETE CASCADE, slot TEXT NOT NULL CHECK (slot IN ('league_banner','event_banner','match_callout')),
 event_id BIGINT REFERENCES events(id) ON DELETE CASCADE, sponsor_id BIGINT REFERENCES sponsors(id) ON DELETE SET NULL, message TEXT NOT NULL DEFAULT '',
 price_cents INT NOT NULL DEFAULT 0, status TEXT NOT NULL DEFAULT 'available' CHECK (status IN ('available','requested','approved','rejected','ended')),
 impressions BIGINT NOT NULL DEFAULT 0, clicks BIGINT NOT NULL DEFAULT 0, created_at TIMESTAMPTZ NOT NULL DEFAULT now());
CREATE TABLE IF NOT EXISTS invoices (
 id BIGSERIAL PRIMARY KEY, league_slug TEXT NOT NULL REFERENCES leagues(slug) ON DELETE CASCADE, sponsor_id BIGINT NOT NULL REFERENCES sponsors(id),
 placement_id BIGINT REFERENCES placements(id) ON DELETE SET NULL, amount_cents INT NOT NULL CHECK (amount_cents > 0),
 status TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open','paid','void')), order_id BIGINT REFERENCES orders(id), note TEXT NOT NULL DEFAULT '',
 created_at TIMESTAMPTZ NOT NULL DEFAULT now(), paid_at TIMESTAMPTZ);
"""),
]


class Db:
    def __init__(self, dsn, *, min_size=1, max_size=6):
        self.pool = ConnectionPool(dsn, min_size=min_size, max_size=max_size, kwargs={"row_factory": dict_row, "autocommit": True}, open=True)

    def close(self):
        self.pool.close()

    def one(self, sql, params=()):
        with self.pool.connection() as c:
            return c.execute(sql, params).fetchone()

    def all(self, sql, params=()):
        with self.pool.connection() as c:
            return c.execute(sql, params).fetchall()

    def run(self, sql, params=()):
        with self.pool.connection() as c:
            return c.execute(sql, params).rowcount

    @contextmanager
    def tx(self):
        with self.pool.connection() as c:
            with c.transaction():
                yield Tx(c)

    def migrate(self):
        with self.pool.connection() as c:
            c.execute("SELECT pg_advisory_lock(727002)")
            try:
                c.execute("CREATE TABLE IF NOT EXISTS rap_migrations (id TEXT PRIMARY KEY, applied_at TIMESTAMPTZ NOT NULL DEFAULT now())")
                done = {r["id"] for r in c.execute("SELECT id FROM rap_migrations").fetchall()}
                for mid, sql in MIGRATIONS:
                    if mid in done:
                        continue
                    with c.transaction():
                        c.execute(sql)
                        c.execute("INSERT INTO rap_migrations(id) VALUES (%s)", (mid,))
            finally:
                c.execute("SELECT pg_advisory_unlock(727002)")


class Tx:
    def __init__(self, conn):
        self.c = conn

    def one(self, sql, params=()):
        return self.c.execute(sql, params).fetchone()

    def all(self, sql, params=()):
        return self.c.execute(sql, params).fetchall()

    def run(self, sql, params=()):
        return self.c.execute(sql, params).rowcount


def audit(db, actor, action, detail=None, league=None):
    db.run("INSERT INTO audit_log(actor_user_id, league_slug, action, detail) VALUES (%s,%s,%s,%s)", (actor, league, action, Jsonb(detail or {})))


def notify(db, user_id, message):
    db.run("INSERT INTO notifications(user_id, message) VALUES (%s,%s)", (user_id, message))


UniqueViolation = psycopg.errors.UniqueViolation
