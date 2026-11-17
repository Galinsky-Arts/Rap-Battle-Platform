# Rap-Battle-Platform

A separate multi-tenant, white-label rap battle and local-league platform built on Live-Competition-Engine; multiple promoter-operated leagues must be independently branded, owned and operated. The platform cannot be implemented as MMS styling or a hardcoded engine mode.

**Complete development plan:** [docs/DEVELOPMENT_PLAN.md](docs/DEVELOPMENT_PLAN.md).

Designed for [Live Competition Engine](https://github.com/neophilism/Live-Competition-Engine); ships with a built-in engine behind the same adapter boundary while that repository is inaccessible.

## End-to-End Everywhere progress

The canonical [security/e2eesa-status.json](security/e2eesa-status.json) records standard implementation, complete testing and independent expert review separately. Engine Room reads it automatically from GitHub. All initial stages are pending; existing profile locks and approved decisions remain authoritative. Read [adoption and independent-account instructions](docs/E2EESA_ADOPTION.md), and run `python3 scripts/validate_e2eesa_status.py`. Record validation does not establish product security or certification.

## Application shell

RAP-01 implementation and operation: [docs/RAP_01_SHELL.md](docs/RAP_01_SHELL.md).
Run `sh scripts/check_application.sh` for the isolated application checks.
The shell's fixtures are synthetic; live engine interoperability and production deployment remain unverified.


## Application status (2026-11-17)

A complete multi-tenant web app in one Python service on Postgres: real accounts and roles, a built-in battle engine behind an adapter (events, rounds, turn timers, judges with rubric scoring, crowd votes with anti-abuse limits, tie rules, brackets, results, archive), emcee profiles with consent, league discovery, seasons and standings, disputes, sponsorship, revenue splits and Stripe Checkout. See [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md).

**Switched off by default:** emcee sign-up (opens only after an admin enters the privacy policy) and payments (need `STRIPE_SECRET_KEY` and `STRIPE_WEBHOOK_SECRET`). Live-Competition-Engine itself remains unconnected; the built-in engine is used.
