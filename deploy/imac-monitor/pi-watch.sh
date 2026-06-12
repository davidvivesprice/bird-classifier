#!/bin/bash
# pi-watch — iMac-side external monitor for the Pi 5 bird observatory.
#
# Why this exists: the Pi's RTL9210 USB-NVMe bridge wedges (4 incidents,
# May-June 2026), leaving a "page-cache zombie": host pings, but anything
# touching disk fails. The Pi cannot be trusted to report its own death,
# so the iMac checks a DISK-TOUCHING endpoint (GET / serves pi_dash.html
# from the NVMe) every 5 minutes and raises a macOS notification on state
# change. Rate-limited re-alerts while the bad state persists.
#
# States:
#   OK        HTTP 200 from /
#   DEGRADED  HTTP up but / not 200 (classic zombie: 500)
#   DOWN      no HTTP response at all (reboot, power, network)
#
# Installed by launchd: ~/Library/LaunchAgents/com.vives.pi-watch.plist
# Log: ~/Library/Logs/pi-watch.log   State: ~/.pi-watch-state

HOSTS=("pi5.local" "192.168.6.156")
URL_PATH=":8099/"
LOG="$HOME/Library/Logs/pi-watch.log"
STATE_FILE="$HOME/.pi-watch-state"
REALERT_S=1800   # re-notify every 30 min while bad

now_epoch=$(date +%s)
ts() { date '+%Y-%m-%d %H:%M:%S'; }

# Probe: first host that answers TCP wins; record HTTP code of /
code="000"
for h in "${HOSTS[@]}"; do
  c=$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 "http://${h}${URL_PATH}" 2>/dev/null)
  if [ "$c" != "000" ]; then code="$c"; break; fi
done

if [ "$code" = "200" ]; then
  state="OK"
elif [ "$code" = "000" ]; then
  state="DOWN"
else
  state="DEGRADED"
fi

prev_state="OK"; prev_alert=0
[ -f "$STATE_FILE" ] && read -r prev_state prev_alert < "$STATE_FILE"
[ -z "$prev_alert" ] && prev_alert=0

echo "$(ts) state=$state http=$code prev=$prev_state" >> "$LOG"

notify() {
  /usr/bin/osascript -e "display notification \"$2\" with title \"$1\" sound name \"Basso\"" 2>/dev/null
  echo "$state $now_epoch" > "$STATE_FILE"
}

if [ "$state" != "OK" ]; then
  if [ "$state" != "$prev_state" ]; then
    if [ "$state" = "DEGRADED" ]; then
      notify "Pi ZOMBIE suspected" "Dashboard / returned HTTP $code — disk-touching endpoint failing. Likely NVMe bridge drop. Power-cycle may be needed (watchdog should self-heal in ~1 min)."
    else
      notify "Pi DOWN" "No HTTP response from pi5. Reboot in progress, power, or network."
    fi
  elif [ $((now_epoch - prev_alert)) -ge $REALERT_S ]; then
    notify "Pi still $state" "Condition persists since last alert (HTTP $code)."
  else
    echo "$prev_state $prev_alert" > "$STATE_FILE"
  fi
else
  if [ "$prev_state" != "OK" ]; then
    notify "Pi recovered" "Dashboard answering HTTP 200 again."
  fi
  echo "OK 0" > "$STATE_FILE"
fi
