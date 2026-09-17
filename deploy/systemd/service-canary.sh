#!/bin/bash
# service-canary — self-heal for the "zombie" failure family (June 2026).
# Runs every 2 min (SYSTEM timer, root). Checks:
#   dashboard HTTP        -> 3+ consecutive fails: restart bird-dashboard
#                            (re-armed: retries every 3rd cycle, not just once)
#   sshd banner           -> 3 consecutive fails: restart ssh.service
#   both wedged 6x        -> reboot
#   pipeline wedge        -> daytime frames_processed frozen 8 consecutive
#                            checks (~16 min): restart bird-pipeline (user unit)
#                            — the live-but-frozen failure class that cost the
#                            iMac 27 days of classifications (Jun-Jul 2026).
# State in /run/service-canary (tmpfs, resets on boot). Logs to journal.
# DRY_RUN=1 prints intended remedies instead of executing them.
S=/run/service-canary; mkdir -p "$S"
DRY="${DRY_RUN:-0}"
act() { if [ "$DRY" = "1" ]; then logger -t service-canary "DRY_RUN: would run: $*"; echo "DRY_RUN: would run: $*"; else "$@"; fi; }
fail() { n=$(($(cat "$S/$1" 2>/dev/null || echo 0)+1)); echo $n > "$S/$1"; echo $n; }
ok()   { echo 0 > "$S/$1"; }

dash=$(curl -s -o /dev/null -w "%{http_code}" --max-time 15 http://localhost:8099/ 2>/dev/null)
if [ "$dash" = "200" ]; then ok dash; dn=0; else dn=$(fail dash); logger -t service-canary "dashboard check failed (http=$dash, consecutive=$dn)"; fi

sshb=$(timeout 10 bash -c "exec 3<>/dev/tcp/127.0.0.1/22 && head -c4 <&3" 2>/dev/null)
if [ "${sshb:0:3}" = "SSH" ]; then ok sshd; sn=0; else sn=$(fail sshd); logger -t service-canary "sshd banner check failed (consecutive=$sn)"; fi

if [ "${dn:-0}" -ge 6 ] && [ "${sn:-0}" -ge 6 ]; then
  logger -t service-canary "ESCALATION: both wedged 6x — rebooting"
  act systemctl reboot
elif [ "${dn:-0}" -ge 3 ] && [ $((dn % 3)) -eq 0 ]; then
  # -ge with modulo: re-arm every 3rd cycle instead of firing exactly once
  # per outage and then going passive while the counter climbs past 3.
  logger -t service-canary "restarting bird-dashboard (user unit, consecutive=$dn)"
  act systemctl --user -M vives@ restart bird-dashboard.service || \
    act su - vives -c "XDG_RUNTIME_DIR=/run/user/1000 systemctl --user restart bird-dashboard"
elif [ "${sn:-0}" -eq 3 ]; then
  logger -t service-canary "restarting ssh.service"
  act systemctl restart ssh.service
fi

# ── Pipeline wedge check ──────────────────────────────────────────────────
# Only meaningful while the pipeline is AWAKE: the solar nighttime pause
# legitimately freezes capture. The gate is the health payload's own
# "night" flag — NOT a clock window. (A fixed 07-19 window, written when
# sunset was 20:30, restarted a healthy paused pipeline every evening from
# mid-September on as the pause crept earlier into the window.)
HEALTH_URL="${CANARY_HEALTH_URL:-http://localhost:8100/api/pipeline/health}"
health=$(curl -s --max-time 10 "$HEALTH_URL" 2>/dev/null)
night=$(printf '%s' "$health" | grep -o '"night": *[a-z]*' | head -1 | grep -o '[a-z]*$')
if [ -n "$health" ] && [ "$night" != "true" ]; then
  fp=$(printf '%s' "$health" \
       | grep -o '"frames_processed": *[0-9]*' | head -1 | grep -o '[0-9]*$')
  if [ -n "$fp" ]; then
    prev=$(cat "$S/pipe_fp" 2>/dev/null || echo "")
    echo "$fp" > "$S/pipe_fp"
    if [ "$fp" = "$prev" ]; then
      pw=$(fail pipe_static)
      if [ "$pw" -ge 8 ]; then
        logger -t service-canary "ESCALATION: pipeline frames_processed frozen at $fp for $pw checks (~$((pw*2)) min, daytime) — restarting bird-pipeline"
        act systemctl --user -M vives@ restart bird-pipeline.service || \
          act su - vives -c "XDG_RUNTIME_DIR=/run/user/1000 systemctl --user restart bird-pipeline"
        ok pipe_static
      elif [ "$pw" -ge 4 ]; then
        logger -t service-canary "pipeline frames_processed static at $fp (consecutive=$pw)"
      fi
    else
      ok pipe_static
    fi
  fi
  # Health endpoint unreachable/no counter: process death is systemd
  # Restart=always territory — the canary only hunts live-but-frozen.
fi

# ── Alerts (no restarts — make a silent failure visible) ─────────────────
# Written where bird-alert.sh already records unit failures so one log
# holds everything a human needs to look at.
ALOG=/home/vives/logs/unit-failures.log
alert() {  # alert <name> <message>
  ts=$(date -Is)
  logger -t service-canary "ALERT $1: $2"
  echo "$ts ALERT $1 $2" >> "$ALOG"
  printf '{"alert": "%s", "message": "%s", "at": "%s"}\n' "$1" "$2" "$ts" \
    > /home/vives/logs/canary-alert-latest.json
  chown vives:vives "$ALOG" /home/vives/logs/canary-alert-latest.json 2>/dev/null
}

# ── iMac liveness (from outside the Mac) ─────────────────────────────────
# Aug 7 → Sep 16 2026 the iMac's launchd itself hung: KeepAlive, the
# in-session watchdog and log rotation all died together and nothing on
# the Mac could notice. 15 consecutive misses (~30 min) → alert; re-alert
# every 6 h while it stays down; one line when it comes back.
IMAC_URL="${CANARY_IMAC_URL:-http://192.168.4.200:8099/api/health}"
ih=$(curl -s -o /dev/null -w "%{http_code}" --max-time 10 "$IMAC_URL" 2>/dev/null)
if [ "$ih" = "200" ]; then
  was=$(cat "$S/imac" 2>/dev/null || echo 0)
  [ "$was" -ge 15 ] && alert imac-liveness "iMac dashboard reachable again after $was missed checks"
  ok imac
else
  im=$(fail imac)
  if [ "$im" -ge 15 ] && [ $(( (im - 15) % 180 )) -eq 0 ]; then
    alert imac-liveness "iMac dashboard unreachable for $im checks (~$((im*2)) min, http=$ih) — check power/launchd"
  fi
fi

# ── Feeder silent: frames flowing, no detections ────────────────────────
# Aug 7 → Sep 10 2026 the Pi ran all day at 15 fps and produced 1-65
# classifications/day (normal 400-1600) with no alarm. Daytime only.
# Sample (epoch, frames_processed, detections_total) every check; if over
# the trailing 4 h frames advanced by >50k but detections by <1000 →
# alert once per day. Counter resets (pipeline restart) skip the check.
if [ -n "$health" ] && [ "$night" != "true" ]; then
  det=$(printf '%s' "$health" | grep -o '"detections_total": *[0-9]*' | head -1 | grep -o '[0-9]*$')
  fpn=$(printf '%s' "$health" | grep -o '"frames_processed": *[0-9]*' | head -1 | grep -o '[0-9]*$')
  if [ -n "$det" ] && [ -n "$fpn" ]; then
    nowe=$(date +%s)
    echo "$nowe $fpn $det" >> "$S/det_hist"
    tail -n 200 "$S/det_hist" > "$S/det_hist.tmp" && mv "$S/det_hist.tmp" "$S/det_hist"
    old=$(awk -v cut=$((nowe - 14400)) '$1 <= cut {l=$0} END {print l}' "$S/det_hist")
    if [ -n "$old" ]; then
      set -- $old; ofp=$2; odet=$3
      today=$(date +%F)
      if [ $((fpn - ofp)) -gt 50000 ] && [ $((det - odet)) -ge 0 ] && [ $((det - odet)) -lt 1000 ] \
         && [ "$(cat "$S/silent_day" 2>/dev/null)" != "$today" ]; then
        echo "$today" > "$S/silent_day"
        alert feeder-silent "$((fpn - ofp)) frames but only $((det - odet)) detections in the last 4 daytime hours — empty feeder, camera aim, or detector?"
      fi
    fi
  fi
fi
