# Rap Battle Platform deployable slice (RAP-01..04)

One Python WSGI service (stdlib app from RAP-01, served by gunicorn) with tenant
data in Postgres (Neon). Live-Competition-Engine is **not** connected — the
upstream repository is not accessible — so a clearly labelled prototype stub
replaces the engine identity adapter. `/readyz` therefore stays 503.

## Delivered

| Unit | What is live |
| --- | --- |
| RAP-01 | Tenant routing by path (`/l/{slug}`, `/leagues/{slug}`, `/api/leagues/{slug}/…`) and by mapped custom domain (Host), exact Host/Origin/HTTPS checks, `/healthz`, CI. |
| RAP-02 | Per-league branding (display name, tagline, four colours with enforced 4.5:1 contrast, optional custom domain) stored in Postgres and rendered through fixed templates and a generated same-origin stylesheet — no per-league fork. |
| RAP-03 | Battle vocabulary (league, battle, round, emcee, judge, champion) used in public and workspace pages. |
| RAP-04 | 1v1, one-round, three-round, crew and tournament presets; written/freestyle; round and overtime timers; tie (overtime/draw/recount), forfeit, no-show, cancel states; seeded brackets with byes; per-league overrides with optimistic versions. |

Three fictional synthetic leagues are seeded when `RAP_SEED_SYNTHETIC=1`:
`/l/ny-synthetic`, `/l/atl-synthetic`, `/l/la-synthetic` (each page bannered
"Synthetic fixture"). They are not real promoters, venues or partnerships.

## Routes

| Route | Access |
| --- | --- |
| `GET /healthz` | Public liveness (Render health check). |
| `GET /readyz` | 503 until a real engine adapter is configured. |
| `GET /l/{slug}`, `/l/{slug}/formats`, `/l/{slug}/brand.css` | Public, tenant-owned pages; only display branding and format summaries. |
| custom domain `/`, `/formats`, `/brand.css` | Same pages, for the one league mapped to that Host; nothing else is served there. |
| `GET /leagues/{slug}` | League workspace; operator token for that league. |
| `GET/PUT /api/leagues/{slug}/settings` | RAP-01 name/timezone. |
| `GET/PUT /api/leagues/{slug}/branding` | RAP-02 branding + custom domain (`{branding, custom_domain, version}`). |
| `GET /api/leagues/{slug}/presets`, `PUT /api/leagues/{slug}/presets/{id}` | RAP-04 presets (`{config, version}`; version 0 creates the override). |

## Prototype identity stub

`StubOperatorEngine` accepts per-league bearer tokens = base64url(HMAC-SHA256(
`RAP_ADMIN_SECRET`, "rap-league-operator:" + organization)). A token for one league
is rejected (403) for every other league. Generate one locally:

```sh
RAP_ADMIN_SECRET=... python -m rapbattle.tokens --organization synthetic-org-ny
```

Setting `RAP_ENGINE_CHECKOUT` switches to the real pinned `NodeEngine` adapter.

## Configuration

| Variable | Purpose |
| --- | --- |
| `DATABASE_URL` | Neon Postgres connection string (or `RAP_DATABASE` SQLite path for local use). |
| `RAP_ADMIN_SECRET` | Generated secret for the stub operator tokens (admin credential). |
| `RAP_ORIGIN` | Canonical HTTPS origin; defaults to Render's `RENDER_EXTERNAL_URL`. |
| `RAP_TRUST_PROXY_PROTO=1` | Trust only `X-Forwarded-Proto: https` from Render's TLS proxy (never Host). |
| `RAP_SEED_SYNTHETIC=1` | Seed the three synthetic leagues idempotently. |
| `RAP_ENGINE_CHECKOUT` | Owner-supplied pinned engine checkout (future). |

Start command: `gunicorn 'rapbattle.runtime:create_from_environment()' --bind 0.0.0.0:$PORT --workers 1 --threads 4`.

## Not in this slice (owner decisions / later units)

No payments, tickets, PPV, tips or payouts (RAP-06/10/11 need a payment provider
and revenue-share policy). No artist personal data or profiles (RAP-05 needs an
owner-approved consent/privacy policy). No league discovery listing (RAP-08).
Tests are AI-authored and AI-reviewed; no independent human audit is claimed.
