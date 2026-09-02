#!/usr/bin/env bash
# The end-to-end thread, as evidence.
#
# Everything below runs without an API key except step 1, which is the one thing
# the brief says cannot be stubbed. Run `tools/seed_artifact.py` first if you
# want the rest without a discovery run; run step 1 to produce the real thing.
#
#   1. discovery      an LLM drives the live app and the run becomes an artifact
#   2. replay         the same artifact, a *different* member
#   3. business outcome   a member that does not exist
#   4. escalation     an irreversible step, unattended -> the gate stops it
#   5. handback       the same step, attended -> a human authorises, run completes
#
# The write capability was recorded opening a $250 sub-account for member 12345;
# the demo opens a $50 one for member 23456, whose accounts could not absorb $250.
#
# Steps 2 and 5 use different parameter values from the recording on purpose.
# Replaying the member the model happened to look up demonstrates nothing about
# parameterisation -- it is indistinguishable from a hardcoded flow.

set -euo pipefail
cd "$(dirname "$0")/.."

PY=${PY:-.venv/Scripts/python.exe}
APP=${APP:-http://127.0.0.1:8099}
# Resolved at use time, not here: re-recording writes v<N+1> beside its
# predecessor rather than over it, so a hardcoded v1 would quietly replay a
# stale artifact after any discovery run -- and the demo would stop
# demonstrating the run that just happened.
latest() { ls -1 "capabilities/acme-servicing/$1"/v*.json | sort -V | tail -1; }
READ_CAP_ID=member.savings_balance.read
WRITE_CAP_ID=member.subaccount.open

: "${SVC_OPERATOR_ID:?set SVC_OPERATOR_ID (the mock app accepts any value)}"
: "${SVC_PASSWORD:?set SVC_PASSWORD}"

curl -sf -X POST "$APP/_reset" >/dev/null || {
  echo "the mock app is not running: $PY mockapp/app.py" >&2; exit 1; }

banner() { printf '\n\033[1m=== %s ===\033[0m\n' "$1"; }

if [[ "${WITH_DISCOVERY:-0}" == "1" ]]; then
  banner "1. discovery — a real LLM run against the live app"
  : "${ANTHROPIC_API_KEY:?set ANTHROPIC_API_KEY for the discovery run}"
  $PY -m cua discover \
    --goal "look up member {member_id} and read their current savings balance" \
    --param member_id=12345 \
    --entry "$APP/servicing/" \
    --capability-id member.savings_balance.read \
    --title "Read member savings balance" \
    --allowlist acme-servicing-readonly
else
  echo "skipping discovery (WITH_DISCOVERY=1 to run it); using seeded artifacts"
  $PY tools/seed_artifact.py >/dev/null
fi

banner "2. replay — same artifact, a different member than was recorded"
$PY -m cua replay --capability "$(latest $READ_CAP_ID)" --param member_id=23456 \
  --allow-draft --run-id rep_read_member_b

banner "3. business outcome — a member that does not exist"
$PY -m cua replay --capability "$(latest $READ_CAP_ID)" --param member_id=99999 \
  --allow-draft --run-id rep_read_not_found

banner "4. escalation — an irreversible step, nobody there to answer"
curl -sf -X POST "$APP/_reset" >/dev/null
$PY -m cua replay --capability "$(latest $WRITE_CAP_ID)" \
  --param member_id=23456 --param opening_deposit=50.00 \
  --param nickname="Holiday Savings" \
  --allow-draft --run-id rep_write_escalated || true

banner "5. handback — the same step, with a human to authorise it"
curl -sf -X POST "$APP/_reset" >/dev/null
$PY -m cua replay --capability "$(latest $WRITE_CAP_ID)" \
  --param member_id=23456 --param opening_deposit=50.00 \
  --param nickname="Holiday Savings" \
  --allow-draft --attended --auto-handback 1 --run-id rep_write_resolved \
  --operator-note "reviewed the transfer against the member record and authorised it"

banner "evidence"
ls -1 evidence/
