#!/bin/bash
# REPO COPY (added 2026-07-17): canonical installed copy is /Users/vives/bin/bird-pipeline-run.sh,
# referenced by launchd. This copy exists so the breaker/monitor/backup
# logic is versioned; keep both in sync when editing (cp to /Users/vives/bin/bird-pipeline-run.sh).
# bird-pipeline-run.sh — crash-loop breaker + Coral auto-degrade for the iMac
# bird pipeline. Invoked by com.vives.bird-pipeline.plist instead of python
# directly.
#
# Root cause (2026-06-15 triage): libedgetpu.1.dylib (Coral Edge TPU) calls
# abort() → SIGABRT when the Coral USB is unstable (typically after a power
# event). That C-level abort uncatchably kills bird_pipeline_v3.py. launchd
# KeepAlive respawns it; if Coral stays bad the result is a crash-loop with
# the observatory DOWN and no alarm. Evidence: DiagnosticReports/
# Python-2026-06-15-035154.ips (faulting thread inside libedgetpu).
#
# This wrapper detects rapid relaunches (the loop) and degrades to AIY-only
# (DISABLE_CORAL=1, honored in bird_pipeline_v3.py) for a cooldown window, so
# the observatory stays up CPU-only instead of looping — and notifies David.
# Normal restarts (days apart) never trip it.

PY=/Users/vives/bird-classifier/venv-coral/bin/python3
APP=/Users/vives/bird-classifier/bird_pipeline_v3.py
STATE_DIR="$HOME/.cache/bird-pipeline-breaker"
HIST="$STATE_DIR/launch-history"
DEGRADE_UNTIL="$STATE_DIR/degrade-until"
LOG="$HOME/bird-snapshots/logs/pipeline-breaker.log"
WINDOW_S=300          # look-back window for loop detection
LOOP_THRESHOLD=3      # >= this many launches within WINDOW_S = crash loop
COOLDOWN_S=3600       # how long to stay AIY-only after a trip

mkdir -p "$STATE_DIR"
now=$(date +%s)
note(){ echo "$(date '+%F %T') $1" >> "$LOG"; }
notify(){ /usr/bin/osascript -e "display notification \"$1\" with title \"iMac bird-pipeline\" sound name \"Basso\"" 2>/dev/null; }

# Record this launch; keep only launches within the window.
echo "$now" >> "$HIST"
tail -100 "$HIST" 2>/dev/null | awk -v n="$now" -v w="$WINDOW_S" '$1 > n-w' > "$HIST.tmp" && mv "$HIST.tmp" "$HIST"
recent=$(wc -l < "$HIST" | tr -d ' ')

# Trip the breaker on a detected loop → start/extend the AIY-only cooldown.
if [ "$recent" -ge "$LOOP_THRESHOLD" ]; then
  echo $((now + COOLDOWN_S)) > "$DEGRADE_UNTIL"
  note "CRASH-LOOP: $recent launches in ${WINDOW_S}s → DISABLE_CORAL for ${COOLDOWN_S}s"
  notify "Crash-loop detected — running AIY-only (no Coral) for 1h. Check/replug the Coral USB."
fi

# Apply degrade if inside the cooldown window.
if [ -f "$DEGRADE_UNTIL" ] && [ "$now" -lt "$(cat "$DEGRADE_UNTIL" 2>/dev/null || echo 0)" ]; then
  export DISABLE_CORAL=1
  note "launch: AIY-only (DISABLE_CORAL=1, cooldown active)"
else
  [ -f "$DEGRADE_UNTIL" ] && { rm -f "$DEGRADE_UNTIL"; note "cooldown expired → re-enabling Coral"; notify "Coral cooldown over — re-enabling yard/Coral on next run."; }
  note "launch: normal (Coral enabled)"
fi

exec "$PY" -u "$APP"
