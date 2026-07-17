#!/bin/bash
# bird-alert.sh <unit> — invoked via OnFailure=bird-alert@%n.service.
# Records the failure where a human (or the dashboard) can see it:
#   ~/logs/unit-failures.log          append-only history
#   ~/logs/unit-failure-latest.json   latest failure, machine-readable
# Deliberately tiny: no network, no restarts — just make the failure visible.
set -u
UNIT="${1:-unknown}"
TS="$(date -Is)"
LOGDIR="$HOME/logs"
mkdir -p "$LOGDIR"
echo "$TS UNIT_FAILED $UNIT" >> "$LOGDIR/unit-failures.log"
printf '{"unit": "%s", "failed_at": "%s"}\n' "$UNIT" "$TS" > "$LOGDIR/unit-failure-latest.json"
logger -t bird-alert "unit failed: $UNIT (recorded in $LOGDIR/unit-failures.log)"
