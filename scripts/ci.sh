#!/usr/bin/env bash
# The verification gate. scripts/verify.py runs it in a copied checkout with its own databases and ports, and the
# release gate (scripts/release.py) runs it directly with one database and no VERIFY_* settings. Keep it compatible
# with bash 3.2 (macOS /bin/bash): no mapfile, no associative arrays, no "wait -n".
set -euo pipefail
cd "$(dirname "$0")/.."
: "${DATABASE_URL:?Set DATABASE_URL to a disposable PostgreSQL database}"
export OWNER_EMAIL="${OWNER_EMAIL:-owner@example.test}"
export PUBLIC_ORIGIN="${TEST_ORIGIN:-http://localhost:${WEB_PORT:-5173}}"
export TEST_ORIGIN="$PUBLIC_ORIGIN"
export TEST_BASE_URL="$PUBLIC_ORIGIN"
export REVIEW_BROWSER_ORIGIN="$PUBLIC_ORIGIN"
export TEST_LOG_DIR="${TEST_LOG_DIR:-$(mktemp -d)}"
export INTERNAL_AUTH_SECRET="test-internal-secret-abcdefghijklmnopqrstuvwxyz"
export BETTER_AUTH_SECRET="test-better-auth-secret-abcdefghijklmnopqrstuvwxyz"
export SIGNUP_MODE=owner-only
export PUBLIC_PROJECTION_ADAPTER=filesystem
export MAIL_MODE=file
# The OAuth test fake (auth/testing/oauth-fake) covers these providers; their placeholder credentials configure them.
# Never set these in a deployment. EMAIL_SIGN_IN stays unset here: stacks set it off below, and the tests that cover
# email codes start their own gateways with EMAIL_SIGN_IN=on.
export GOOGLE_CLIENT_ID=fake-google-client
export GOOGLE_CLIENT_SECRET=fake-google-secret
export DISCORD_CLIENT_ID=fake-discord-client
export DISCORD_CLIENT_SECRET=fake-discord-secret
export MAIL_FILE="$(mktemp)"
export PUBLIC_PROJECTION_DIR="$(mktemp -d)"
export CATALOG_PROCESS_FIXTURE_DIR="$(mktemp -d)"
export PRICE_RUNNER_LOCK_DIR="$(mktemp -d)"
export VERIFY_SUITE="${VERIFY_SUITE:-all}"
export VERIFY_FAST="${VERIFY_FAST:-}"
# K/N from scripts/verify.py --shard: run the K-th of N parts of the api or browser suite's test files.
export VERIFY_SHARD="${VERIFY_SHARD:-}"
export VERIFY_WORK="$(mktemp -d)"
export VERIFY_PYTEST_WORKER_ROOT="$VERIFY_WORK/pytest"
pids=()
stacks=1
cleanup() {
  if [ "${#pids[@]}" -gt 0 ]; then kill "${pids[@]}" 2>/dev/null || true; fi
  rm -f "$MAIL_FILE"
  rm -rf "$PUBLIC_PROJECTION_DIR" "$CATALOG_PROCESS_FIXTURE_DIR" "$PRICE_RUNNER_LOCK_DIR" "$VERIFY_WORK"
}
trap cleanup EXIT
phase() {  # $1: phase name; $2: start or end
  printf '%s\t%s\t%s\n' "$1" "$2" "$(date +%s)" >> "$TEST_LOG_DIR/phases.tsv"
  echo "== verify phase: $1 $2"
}
start_stack() {  # $1: stack directory holding env.sh; $2: log directory
  mkdir -p "$2"
  (. "$1/env.sh"; PYTHONPATH="$PWD/api/tests/catalog_process_fixture:$PWD/api" exec .venv/bin/uvicorn app.main:app --app-dir api --host 127.0.0.1 --port "$API_PORT" --no-access-log) > "$2/api.log" 2>&1 &
  pids+=($!)
  # Every stack's gateway signs in through the OAuth test fake, with email codes off.
  (. "$1/env.sh"; EMAIL_SIGN_IN=off exec node --import tsx --import ./auth/testing/oauth-fake/preload.ts auth/src/server.ts) > "$2/auth.log" 2>&1 &
  pids+=($!)
  (. "$1/env.sh"; exec node node_modules/vite/bin/vite.js --config web/vite.config.ts --strictPort) > "$2/web.log" 2>&1 &
  pids+=($!)
}
wait_for_stack() {  # $1: stack directory holding env.sh
  local origin
  origin=$(. "$1/env.sh"; echo "$TEST_ORIGIN")
  for i in $(seq 1 50); do
    kill -0 "${pids[@]}"
    if curl -fsS "$origin/api/health" >/dev/null 2>&1; then break; fi
    sleep .2
  done
  kill -0 "${pids[@]}"
  curl -fsS "$origin/api/health" >/dev/null
}
case "$VERIFY_SUITE" in
  all|api|browser|auth|operations|burn-in) ;;
  *) echo 'Unknown verification suite' >&2; exit 1 ;;
esac
# Only the api and browser runners take a shard; any other suite would run whole while its caller expected one part.
case "$VERIFY_SUITE:$VERIFY_SHARD" in
  *:|api:*|browser:*) ;;
  *) echo 'VERIFY_SHARD needs the api or browser suite' >&2; exit 1 ;;
esac
phase setup start
.venv/bin/python scripts/migrate.py
.venv/bin/python scripts/init_public_projection.py
.venv/bin/python scripts/backfill_public_catalog.py
# A browser shard is one leg of the split CI gate (.github/workflows/verify.yml): its stacks serve web/ through the vite
# dev server and never read the build output, and the auth leg runs guardrails and the build once for every leg.
case "$VERIFY_SUITE:$VERIFY_SHARD" in
  api:*|browser:?*) ;;
  *) npm run guardrails; npm run build ;;
esac
case "$VERIFY_SUITE" in
  all) .venv/bin/python scripts/verify_resources.py prepare --pytest --stacks ;;
  api) .venv/bin/python scripts/verify_resources.py prepare --pytest ;;
  browser|auth|burn-in) .venv/bin/python scripts/verify_resources.py prepare --stacks ;;
esac
case "$VERIFY_SUITE" in
  all|browser|auth|burn-in)
    stacks=$(cat "$VERIFY_WORK/stacks/count")
    start_stack "$VERIFY_WORK/stacks/s0" "$TEST_LOG_DIR"
    k=1
    while [ "$k" -lt "$stacks" ]; do  # not seq: BSD seq counts down when stacks is 1
      start_stack "$VERIFY_WORK/stacks/s$k" "$TEST_LOG_DIR/stack-$k"
      k=$((k + 1))
    done
    k=0
    while [ "$k" -lt "$stacks" ]; do
      wait_for_stack "$VERIFY_WORK/stacks/s$k"
      k=$((k + 1))
    done
    export VERIFY_BROWSER_STACKS="$(cat "$VERIFY_WORK/stacks/stacks.json")"
    ;;
esac
phase setup end
case "$VERIFY_SUITE" in
  all|api)
    phase pytest start
    .venv/bin/python scripts/run_pytest.py ${VERIFY_FAST:+--fast} ${VERIFY_SHARD:+--shard "$VERIFY_SHARD"} -- "$@"
    phase pytest end
    ;;
esac
# Burn-in (scripts/verify.py --burn-in) names its changed vitest files and Playwright specs in VERIFY_BURN_IN_VITEST and
# VERIFY_BURN_IN_SPECS, space-separated; a suite with none of them is left out.
burn_in_vitest=""
if [ "$VERIFY_SUITE" = burn-in ]; then
  burn_in_vitest="${VERIFY_BURN_IN_VITEST:-}"
  set -- ${VERIFY_BURN_IN_SPECS:-}
fi
case "$VERIFY_SUITE" in
  all|auth|burn-in)
    if [ "$VERIFY_SUITE" != burn-in ] || [ -n "$burn_in_vitest" ]; then
      phase vitest start
      # --fast leaves out vitest files and tests tagged slow (vitest.config.ts); the junit report holds per-test times.
      # VERIFY_VITEST_REPEAT > 1 (--repeat, --burn-in) runs vitest that many times, one after another (repeats of one
      # file would share its ports and the gate's database), each with its own vitest-<k>.xml.
      repeats="${VERIFY_VITEST_REPEAT:-1}"
      vitest_failed=0
      k=1
      while [ "$k" -le "$repeats" ]; do
        report="$TEST_LOG_DIR/vitest.xml"
        if [ "$repeats" -gt 1 ]; then
          report="$TEST_LOG_DIR/vitest-$k.xml"
          echo "== vitest run $k of $repeats"
        fi
        npm run test:auth -- --reporter=default --reporter=junit --outputFile.junit="$report" ${VERIFY_FAST:+--tags-filter=!slow} $burn_in_vitest || vitest_failed=1
        k=$((k + 1))
      done
      phase vitest end
      if [ "$vitest_failed" != 0 ]; then exit 1; fi
    fi
    ;;
esac
case "$VERIFY_SUITE" in
  all|browser|burn-in)
    if [ "$VERIFY_SUITE" != burn-in ] || [ "$#" -gt 0 ]; then
      phase browser start
      .venv/bin/python scripts/run_playwright.py --workers "$stacks" ${VERIFY_FAST:+--fast} --repeat-each "${VERIFY_REPEAT:-1}" \
        ${VERIFY_SHARD:+--shard "$VERIFY_SHARD"} -- "$@"
      phase browser end
    fi
    ;;
esac
case "$VERIFY_SUITE" in
  all|operations)
    # verify.py sets VERIFY_OPERATIONS=0 for --fast; the release gate (scripts/release.py) sets nothing, so it runs.
    if [ "${VERIFY_OPERATIONS:-1}" = 1 ]; then
      phase operations start
      .venv/bin/python -m unittest discover -s tests/operations -v
      phase operations end
    else
      echo "== verify phase: operations skipped (--fast)"
    fi
    ;;
esac
