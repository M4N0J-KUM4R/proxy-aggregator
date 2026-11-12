#!/bin/bash
# refresh_and_publish.sh — the whole proxy pipeline, end to end.
#
#   check in the source repositories
#     -> scrape the lists
#     -> validate every proxy
#     -> categorize the survivors
#     -> merge the result into the router's pool
#     -> publish into the 9router repository and commit it
#     -> reload the running shim
#
# Any step that fails stops the run before the publish, so a broken pipeline
# never replaces a good pool.
#
# Environment:
#   AGG_DIR          the aggregator checkout (default: this script's directory)
#   ROUTER_REPO      the 9router repository to publish into
#                    (default: $HOME/gml/9router-repo)
#   LIVE_ROUTER_DIR  the running stack, whose pool file is refreshed
#                    (default: $HOME/gml/9router)
#   SHIM_URL         the running shim, for the reload (default: :8802)
#   MAX_PROXIES      how many unique proxies to validate (default: 120000)
#   VALIDATOR        path to the proxy-scraper-checker binary
set -uo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$DIR" || exit 1

AGG_DIR="${AGG_DIR:-$DIR}"
ROUTER_REPO="${ROUTER_REPO:-$HOME/gml/9router-repo}"
LIVE_ROUTER_DIR="${LIVE_ROUTER_DIR:-$HOME/gml/9router}"
SHIM_URL="${SHIM_URL:-http://127.0.0.1:8802}"
MAX_PROXIES="${MAX_PROXIES:-120000}"
VALIDATOR="${VALIDATOR:-$AGG_DIR/validator/proxy-scraper-checker}"

WORK="$AGG_DIR/work"
LOG="$WORK/refresh.log"
mkdir -p "$WORK" "$(dirname "$LOG")"

say() { printf '[%s] %s\n' "$(date '+%H:%M:%S')" "$*" | tee -a "$LOG"; }
die() { say "FAILED: $*"; exit 1; }

say "=== proxy pool refresh starting ==="

# ---------------------------------------------------------------- 0. toolchain
PY=""
for cand in "$(command -v python3 2>/dev/null)" \
            /Library/Frameworks/Python.framework/Versions/3.13/bin/python3 \
            /opt/homebrew/bin/python3 /usr/local/bin/python3 /usr/bin/python3; do
  [ -n "$cand" ] && [ -x "$cand" ] || continue
  if "$cand" -c "import requests" >/dev/null 2>&1; then PY="$cand"; break; fi
done
[ -n "$PY" ] || die "no python with the requests module"

if [ ! -x "$VALIDATOR" ]; then
  # The macOS arm64 build sits in ~/Downloads when it was downloaded by hand.
  for cand in "$HOME/Downloads/proxy-scraper-checker-binary-aarch64-apple-darwin/proxy-scraper-checker" \
              "$HOME/Downloads/proxy-scraper-checker-binary-x86_64-apple-darwin/proxy-scraper-checker"; do
    if [ -x "$cand" ]; then
      mkdir -p "$(dirname "$VALIDATOR")"
      cp "$cand" "$VALIDATOR"; chmod +x "$VALIDATOR"
      say "installed the validator from $cand"
      break
    fi
  done
fi
[ -x "$VALIDATOR" ] || die "no validator binary at $VALIDATOR"

# ------------------------------------------------------- 1. check in the repos
# The aggregator's own checkout first, so the scripts and the source manifest
# are current before anything runs.
if [ -d "$AGG_DIR/.git" ]; then
  say "checking out the aggregator"
  git -C "$AGG_DIR" pull --ff-only --quiet 2>&1 | tee -a "$LOG" || say "  (pull reported a problem, continuing with what is here)"
fi

say "checking in the upstream proxy-list repositories"
"$PY" scripts/checkin_sources.py \
  --sources sources/sources.json \
  --work-dir "$WORK" 2>&1 | tee -a "$LOG"
CHECKIN_RC=${PIPESTATUS[0]}
if [ "$CHECKIN_RC" -ne 0 ]; then
  say "  no repository could be checked in; the raw URLs are the fallback"
fi

# ------------------------------------------------------------------ 2. scrape
say "scraping the lists"
"$PY" scripts/fetch_sources.py \
  --sources sources/sources.json \
  --work-dir "$WORK" \
  --repos-dir "$WORK/repos" \
  --max-proxies "$MAX_PROXIES" 2>&1 | tee -a "$LOG"
[ "${PIPESTATUS[0]}" -eq 0 ] || die "the scrape failed"

# ---------------------------------------------------------------- 3. validate
say "validating"
ulimit -n 65535 2>/dev/null || true
( cd "$WORK" && PROXY_SCRAPER_CHECKER_CONFIG="$WORK/config.toml" \
    "$VALIDATOR" 2>&1 | tee -a "$LOG" )
[ -s "$WORK/out/proxies.json" ] || die "the validator produced no output"

# -------------------------------------------------------------- 4. categorize
say "categorizing"
"$PY" scripts/categorize.py --work-dir "$WORK" --output-dir "$AGG_DIR/output" 2>&1 | tee -a "$LOG"
[ "${PIPESTATUS[0]}" -eq 0 ] || die "the categorization failed"

# --------------------------------------------------------- 5. build the pool
say "building the router's pool"
PREVIOUS="$LIVE_ROUTER_DIR/deepai/proxies.txt"
"$PY" scripts/build_router_pool.py \
  --working "$AGG_DIR/output/all.txt" \
  --tested "$WORK/pools" \
  --previous "$PREVIOUS" \
  --out "$WORK/router-proxies.txt" 2>&1 | tee -a "$LOG"
[ "${PIPESTATUS[0]}" -eq 0 ] || die "the pool build failed"
[ -s "$WORK/router-proxies.txt" ] || die "the merged pool is empty"

# --------------------------------------------------------------- 6. publish
if [ -d "$ROUTER_REPO/.git" ]; then
  say "publishing into $ROUTER_REPO"
  mkdir -p "$ROUTER_REPO/proxy-aggregator"
  rm -rf "$ROUTER_REPO/proxy-aggregator/output"
  cp -R "$AGG_DIR/output" "$ROUTER_REPO/proxy-aggregator/output"
  cp -R "$AGG_DIR/scripts" "$ROUTER_REPO/proxy-aggregator/scripts"
  cp -R "$AGG_DIR/sources" "$ROUTER_REPO/proxy-aggregator/sources"
  cp "$AGG_DIR/README.md" "$ROUTER_REPO/proxy-aggregator/README.md"
  find "$ROUTER_REPO/proxy-aggregator" -name ".DS_Store" -delete
  cp "$WORK/router-proxies.txt" "$ROUTER_REPO/deepai/proxies.txt"

  # the running stack reads the same file
  if [ -d "$LIVE_ROUTER_DIR/deepai" ]; then
    cp "$WORK/router-proxies.txt" "$LIVE_ROUTER_DIR/deepai/proxies.txt"
    say "updated the live pool at $LIVE_ROUTER_DIR/deepai/proxies.txt"
  fi

  cd "$ROUTER_REPO" || exit 1
  git add -A proxy-aggregator deepai/proxies.txt
  if git diff --cached --quiet; then
    say "no change in the repository"
  else
    TOTAL=$(python3 -c "import json;print(json.load(open('$AGG_DIR/output/summary.json')).get('total_working','?'))" 2>/dev/null || echo '?')
    POOL=$(wc -l < "$WORK/router-proxies.txt" | tr -d ' ')
    git -c user.name="M4N0J-KUM4R" -c user.email="m4n0j-kum4r@users.noreply.github.com" \
        commit -q -m "chore: refresh the proxy pool (${TOTAL} working, ${POOL} in the router pool)"
    git push -q origin HEAD 2>&1 | tee -a "$LOG" || say "  the push failed; the commit is local"
    say "committed and pushed the refreshed pool"
  fi
else
  say "no repository at $ROUTER_REPO - the pool was not published"
fi

# ------------------------------------------------------------- 7. hot reload
if curl -fsS --max-time 20 -X POST "$SHIM_URL/reload" >/dev/null 2>&1; then
  say "the running shim reloaded its pool"
else
  say "the shim at $SHIM_URL did not reload (it may not be running); it will pick the pool up on its next start"
fi

say "=== done ==="
