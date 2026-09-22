#!/usr/bin/env bash
# One command, and the secret scanner runs before every commit and push.
#
# Uses core.hooksPath so the hooks are VERSIONED — a hook living only in
# .git/hooks protects exactly one clone, which is how a guard quietly stops
# existing for everyone else.
set -e
root="$(git rev-parse --show-toplevel)"
git -C "$root" config core.hooksPath .githooks
chmod +x "$root/.githooks/"*
echo "hooks installed: $(git -C "$root" config core.hooksPath)"
echo
echo "  pre-commit  blocks a commit that stages a credential"
echo "  pre-push    scans the commits about to be pushed"
echo
echo "Before making a repository public, also run:"
echo "  python3 tools/scan_secrets.py --history"
