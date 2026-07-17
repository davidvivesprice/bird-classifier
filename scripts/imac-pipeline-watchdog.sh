#!/bin/bash
# imac-pipeline-watchdog.sh — detects the live-but-wedged pipeline failure.
#
# Why: launchd KeepAlive only catches process DEATH. On 2026-06-20 the iMac
# pipeline froze while staying alive (health counters cached, ffmpeg stall
# storm) and 27 days of classifications were silently lost. This watchdog is
# the external liveness check that failure class needs: if the newest row in
# classifications.db is older than 3 h during daylight while the pipeline
# process has been up at least that long, kickstart it and notify.
#
# Runs every 30 min via com.vives.bird-pipeline-watchdog.plist.
# DRY_RUN=1 reports what it would do without acting.
set -u
DB="${WATCHDOG_DB:-/Users/vives/bird-snapshots/logs/classifications.db}"   # override for testing
LABEL=com.vives.bird-pipeline
MAX_AGE_S="${WATCHDOG_MAX_AGE_S:-$((3*3600))}"   # override for testing/tuning
LOG(){ echo "$(date '+%F %T') $*"; }

hour=$((10#$(date +%H)))
if [ "$hour" -lt 7 ] || [ "$hour" -gt 19 ]; then
  LOG "night (hour=$hour) — capture legitimately idle, skip"
  exit 0
fi

pid=$(launchctl list | awk -v l="$LABEL" '$3==l {print $1}')
if [ -z "$pid" ] || [ "$pid" = "-" ]; then
  LOG "pipeline not running — process death is launchd KeepAlive territory, skip"
  exit 0
fi

# Uptime gate: never judge (or restart-loop) a pipeline younger than the
# staleness window — a fresh restart needs time to classify its first bird.
et=$(ps -o etime= -p "$pid" | tr -d ' ')
case "$et" in
  *-*)   up_s=$((MAX_AGE_S+1)) ;;   # DD-HH:MM:SS — days old, certainly enough
  *:*:*) IFS=: read -r h m s <<<"$et"; up_s=$((10#$h*3600 + 10#$m*60 + 10#$s)) ;;
  *)     IFS=: read -r m s <<<"$et";  up_s=$((10#$m*60 + 10#$s)) ;;
esac
if [ "$up_s" -lt "$MAX_AGE_S" ]; then
  LOG "pipeline uptime ${up_s}s < ${MAX_AGE_S}s — too young to judge, skip"
  exit 0
fi

# timestamps in classifications are LOCAL wall-clock; compare local-to-local
# ('now','localtime' mislabels local as UTC exactly like the stored value,
# so the difference is correct).
age=$(sqlite3 "file:$DB?mode=ro" \
  "SELECT CAST(strftime('%s','now','localtime') - strftime('%s', MAX(timestamp)) AS INTEGER) FROM classifications" 2>/dev/null)
if [ -z "$age" ]; then
  LOG "could not read newest classification age from $DB — skip"
  exit 0
fi

if [ "$age" -gt "$MAX_AGE_S" ]; then
  LOG "WEDGE: newest classification ${age}s old (>${MAX_AGE_S}s) while pipeline up ${up_s}s (pid $pid)"
  if [ "${DRY_RUN:-0}" = "1" ]; then
    LOG "DRY_RUN: would run: launchctl kickstart -k gui/$(id -u)/$LABEL"
  else
    launchctl kickstart -k "gui/$(id -u)/$LABEL" && LOG "kickstarted $LABEL"
    /usr/bin/osascript -e 'display notification "Pipeline was alive but frozen (no classifications for 3h+ in daylight) — kickstarted it." with title "iMac bird-watchdog" sound name "Basso"' 2>/dev/null
  fi
else
  LOG "ok: newest classification ${age}s old (pipeline up ${up_s}s)"
fi
