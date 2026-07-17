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
# Only meaningful in daytime: nighttime pause legitimately freezes capture
# (frames_processed is flat ~21:00-05:00). Gate to 07-19 local for margin.
hour=$((10#$(date +%H)))
HEALTH_URL="${CANARY_HEALTH_URL:-http://localhost:8100/api/pipeline/health}"
if [ "$hour" -ge 7 ] && [ "$hour" -le 19 ]; then
  fp=$(curl -s --max-time 10 "$HEALTH_URL" 2>/dev/null \
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
