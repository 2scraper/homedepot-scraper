#!/usr/bin/env bash
# Measure which of the three walls homedepot.com is serving THIS exit, right
# now, across N session-pinned addresses.
#
# This replaces the AWS WAF probes carried over from a sibling repo. This site
# does not use AWS WAF, and a tool that reports on a vendor that is not there
# misleads rather than informs.
#
#   tools/edge_probe.sh 6
#   tools/edge_probe.sh 6 'https://www.homedepot.com/b/.../N-...'
#
# Reads HOMEDEPOT_PROXY from .env. The credential never reaches argv.
#
# What you are looking at:
#   ~1.3 MB, 200   content — this exit is fine
#   ~2.5 KB, 200   Akamai's behavioural interstitial: a JS sensor, NOT a
#                  captcha. A browser clears it; curl never will.
#   367 B,   403   Akamai's address refusal
#   2410 B,  403   Home Depot's own branded error page
set -u

COUNT="${1:-5}"
URL="${2:-https://www.homedepot.com/b/Tools-Power-Tools-Saws-Miter-Saws/N-5yc1vZc2d7}"
ENV_FILE="$(dirname "$0")/../.env"

if [ ! -f "$ENV_FILE" ]; then
  echo "no .env beside the repo root — see .env.example" >&2
  exit 2
fi
BASE=$(grep -E '^HOMEDEPOT_PROXY=' "$ENV_FILE" | head -1 | cut -d= -f2-)
if [ -z "$BASE" ]; then
  echo "HOMEDEPOT_PROXY is empty in .env. Measured 2026-09-22: every URL on" >&2
  echo "www.homedepot.com answers 403 from a non-US address." >&2
  exit 2
fi

UA='Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36'

printf '%-10s %-8s %-10s %s\n' session status bytes verdict
for i in $(seq 1 "$COUNT"); do
  # Session-pin the credential without ever printing it.
  SESSION="probe$i$$"
  # Session-pinned through the repo's own helper rather than a sed on the URL:
  # the first version used `s/(-session-[^:@]*)?(:)/.../` and matched the colon
  # in `http://`, producing a proxy URL curl could not use at all (every probe
  # reported status 000, which reads like a dead site rather than a broken
  # argument).
  PROXY=$(cd "$(dirname "$0")/.." && python3 -c '
import sys, proxy_pool
print(proxy_pool.with_session(sys.argv[1], sys.argv[2]))
' "$BASE" "$SESSION")
  OUT=$(mktemp)
  CODE=$(curl -sS -L --proxy "$PROXY" -o "$OUT" -w '%{http_code}' --max-time 60 \
    -H 'accept: text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8' \
    -H 'accept-language: en-US,en;q=0.9' \
    -H 'sec-ch-ua: "Chromium";v="140", "Not=A?Brand";v="24", "Google Chrome";v="140"' \
    -H 'sec-ch-ua-mobile: ?0' -H 'sec-ch-ua-platform: "macOS"' \
    -H 'sec-fetch-dest: document' -H 'sec-fetch-mode: navigate' \
    -H 'sec-fetch-site: none' -H 'sec-fetch-user: ?1' \
    -H 'upgrade-insecure-requests: 1' -H "user-agent: $UA" \
    --compressed "$URL" 2>/dev/null) || CODE=000
  SIZE=$(wc -c < "$OUT" | tr -d ' ')
  VERDICT=$(cd "$(dirname "$0")/.." && python3 -c '
import sys
import product_parser as P
print(P.detect_page_state(open(sys.argv[1], encoding="utf-8", errors="replace").read()))
' "$OUT" 2>/dev/null || echo unreadable)
  printf '%-10s %-8s %-10s %s\n' "$SESSION" "$CODE" "$SIZE" "$VERDICT"
  rm -f "$OUT"
done
