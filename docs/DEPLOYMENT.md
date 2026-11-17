# Rap Battle Platform deployment and operations

One Python WSGI service served by gunicorn, on Postgres (Neon). The RAP-01..04 store creates the
league tables; `rapbattle/web/db.py` migrations (recorded in `rap_migrations`, advisory-locked) add
everything else at boot. Tests: `sh scripts/check_application.sh` (set `TEST_DATABASE_URL` for the
Postgres and RAP-12 end-to-end tests).

Start command: `gunicorn 'rapbattle.runtime:create_from_environment()' --bind 0.0.0.0:$PORT --workers 1 --threads 4`.

## What the app does

| Area | Routes |
| --- | --- |
| Platform | `/` (upcoming battles, leagues), `/leagues` (discovery: search, city/region), `/privacy` |
| Accounts | `/register`, `/login`, `/logout`, `/account` (notifications, emcee profiles, contracts, purchases, password, delete account) |
| League (branded with its own stylesheet) | `/l/{slug}`, `/l/{slug}/events`, `/l/{slug}/events/{event}`, `/l/{slug}/battles/{id}` (live timer, crowd vote, dispute), `/l/{slug}/artists`, `/l/{slug}/artists/{id}`, `/l/{slug}/standings`, `/l/{slug}/formats` |
| Emcees (RAP-05/06) | `/artist/signup`, profile edit, media links, photo upload (cap in settings), visibility, consents, payout consent, lineup requests, paid media |
| Judges | `/judge`, `/judge/battles/{id}` — rubric scores per round and side |
| League admin (RAP-07/09/10/11) | `/manage/{slug}` settings, staff, seasons · `/events` · event detail (rubric, lineup requests, lineup, seeding from standings, judges, bracket or single battles, finalize) · `/battles/{id}` live console (start, turn timer, crowd vote, close/decide, forfeit, cancel, correction, recording) · `/artists` moderation and contracts · `/disputes` · `/sponsors` · `/revenue` |
| Platform admin | `/admin` (leagues, create league), `/admin/settings` (privacy policy, sign-up switch, vote and upload limits, payments status, retention), `/admin/users`, `/admin/audit` |
| Payments | `POST /pay/checkout`, `/sponsor/invoices/{id}`, `/pay/success`, `/pay/cancel`, `POST /stripe/webhook` |
| Legacy RAP-01..04 | `/healthz`, `/readyz`, `/api/leagues/{slug}/…` (per-league bearer tokens from `RAP_ADMIN_SECRET`), `/leagues/{slug}`, custom-domain pages |

## Battle engine

`rapbattle/engine/builtin.py` implements the adapter contract (`ENGINE_METHODS`). Matches follow the
RAP-04 preset snapshot taken when the event is created: rounds, round and overtime timers, alternating
turn order, judges, tie policy. Each judge's ballot goes to the side with the higher weighted rubric
total over the counted rounds (overtime rounds only, after a tie). When the format enables it, the
crowd's majority counts as one extra ballot. Ties: `overtime-round` (up to the preset's limit, then
draw), `judges-recount` (scores cleared, judges re-score) or `draw`. Brackets are seeded
single-elimination with byes; winners advance automatically and corrections are blocked once the
next battle has started. `RAP_ENGINE=external` selects a fail-closed placeholder for a future
Live-Competition-Engine client.

Crowd-vote anti-abuse: signed-in accounts only, minimum account age (default 10 minutes), one vote
per account per battle, per-network cap per battle (keyed HMAC of the IP, default 25), emcees cannot
vote in their own battle, judges cannot vote, per-IP rate limit.

## Money

Revenue kinds: ticket, PPV, tip, membership (30 days), paid media, sponsor invoice. Default splits
(platform / league / artist / event pool): ticket and PPV 10/60/0/30, tip 10/0/90/0, membership and paid
media 10/10/80/0, sponsorship 15/85/0/0 — each league can change them (rows must total 100%).
Paid orders write ledger lines; refunds and chargebacks (from Stripe webhooks) write proportional
negative lines. Settlements close all unsettled lines, divide event pools equally among the event's
emcees, hold artist shares without payout consent, and record a SHA-256 evidence hash.

## Switched off by default

* **Emcee sign-up is CLOSED** until a platform admin enters operator name, privacy contact,
  retention days, policy version and policy text in Admin → Settings and presses "Open emcee sign-up".
  Editing the policy while open closes sign-up again.
* **Payments are OFF** until both `STRIPE_SECRET_KEY` and `STRIPE_WEBHOOK_SECRET` are set. Then add a
  Stripe webhook to `https://<host>/stripe/webhook` for `checkout.session.completed`,
  `checkout.session.expired`, `charge.refunded`, `charge.dispute.created`. Prices are still shown;
  offline sponsor payments can be recorded.
* Sponsorship placements appear only in leagues that opt in.

## Configuration

| Variable | Purpose |
| --- | --- |
| `DATABASE_URL` | Neon Postgres (required for the full app; without it only the legacy RAP-01..04 slice runs on SQLite via `RAP_DATABASE`). |
| `RAP_ADMIN_SECRET` | Generated. Bootstrap admin password (sign in with email `admin`) and signer of legacy per-league API tokens. |
| `RAP_SECRET_KEY` | Generated. CSRF and IP-hash keys (falls back to `RAP_ADMIN_SECRET`). |
| `RAP_ORIGIN` | Canonical https origin; defaults to `RENDER_EXTERNAL_URL`. |
| `RAP_TRUST_PROXY_PROTO=1` | Behind Render's TLS proxy (also trusts the first `X-Forwarded-For` hop for rate limits). |
| `RAP_SEED_SYNTHETIC=1` | Seeds the three fictional leagues. |
| `STRIPE_SECRET_KEY`, `STRIPE_WEBHOOK_SECRET` | Owner-supplied; turn payments on. |
| `RAP_ENGINE`, `RAP_ENGINE_ORIGIN`, `RAP_ENGINE_CHECKOUT` | Optional engine selection (default `builtin`). |

## First run

1. Sign in at `/login` as `admin` with `RAP_ADMIN_SECRET`.
2. Create leagues (or use the synthetic ones), add promoters and judges (Admin → Users, or each league's dashboard).
3. Promoters create seasons and events from presets, publish them, build lineups and run battles from the console.
4. When the owner approves the privacy policy, enter it and open emcee sign-up; add Stripe keys to take payments.

Tests are AI-authored and AI-reviewed; no independent human audit is claimed.
