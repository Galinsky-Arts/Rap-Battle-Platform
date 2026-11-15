"""Print a prototype league-operator token (StubOperatorEngine) for one league.

Usage: RAP_ADMIN_SECRET=... python -m rapbattle.tokens --organization synthetic-org-ny
The secret is read from the environment only; the token is printed to stdout and
must be handled like a password. This tool is not an upstream identity issuer.
"""
import argparse
import os
import sys

from .identity import StubOperatorEngine


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--organization", required=True, help="engine organization id mapped to the league")
    args = parser.parse_args(argv)
    secret = os.environ.get("RAP_ADMIN_SECRET")
    if not secret:
        print("RAP_ADMIN_SECRET is not set", file=sys.stderr)
        return 2
    print(StubOperatorEngine(secret).token_for(args.organization))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
