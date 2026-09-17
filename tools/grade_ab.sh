#!/usr/bin/env bash
# grade_ab.sh — on-Pi A/B of tracker implementations through the REAL
# pipeline (Hailo detector + AIY classifier + vote-lock) on the annotated
# may10 reel: `tools/lab grade` once per tracker, results side by side.
#
#   tools/grade_ab.sh            # v3 then v4 (default env), ~3 min each
#   TRACKERS="v4" tools/grade_ab.sh
#
# Stops/starts bird-pipeline around each run (lab does this); run from the
# Mac (drives the Pi over ssh) or on the Pi.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
TRACKERS="${TRACKERS:-v3 v4}"
OUT="${OUT:-/tmp/grade_ab}"
mkdir -p "$OUT"
for T in $TRACKERS; do
  echo "══ grading tracker=$T ══"
  LAB_ENV="PIPELINE_TRACKER=$T" "$HERE/lab" grade 2>&1 | tee "$OUT/grade_$T.txt"
done
echo
echo "══ side by side (tracker lines) ══"
for T in $TRACKERS; do
  echo "-- $T --"
  grep -E "fragmentation|duplicate-box|id.switch|census|CORRECT|windows covered|phantom" "$OUT/grade_$T.txt" | head -12
done
