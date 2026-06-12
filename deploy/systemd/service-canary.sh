#!/bin/bash
# service-canary — self-heal for the "zombie" failure family (June 2026).
# Checks every 2 min (timer): dashboard HTTP + sshd banner. Escalation:
#   3 consecutive dashboard fails -> restart bird-dashboard (user unit)
#   3 consecutive sshd fails      -> restart ssh.service
#   6 consecutive both-fail       -> reboot
# State in /run/service-canary (tmpfs, resets on boot). Logs to journal.
S=/run/service-canary; mkdir -p "$S"
fail() { n=$(($(cat "$S/$1" 2>/dev/null || echo 0)+1)); echo $n > "$S/$1"; echo $n; }
ok()   { echo 0 > "$S/$1"; }

dash=$(curl -s -o /dev/null -w "%{http_code}" --max-time 15 http://localhost:8099/ 2>/dev/null)
if [ "$dash" = "200" ]; then ok dash; dn=0; else dn=$(fail dash); logger -t service-canary "dashboard check failed (http=$dash, consecutive=$dn)"; fi

sshb=$(timeout 10 bash -c "exec 3<>/dev/tcp/127.0.0.1/22 && head -c4 <&3" 2>/dev/null)
if [ "${sshb:0:3}" = "SSH" ]; then ok sshd; sn=0; else sn=$(fail sshd); logger -t service-canary "sshd banner check failed (consecutive=$sn)"; fi

if [ "${dn:-0}" -ge 6 ] && [ "${sn:-0}" -ge 6 ]; then
  logger -t service-canary "ESCALATION: both wedged 6x — rebooting"
  systemctl reboot
elif [ "${dn:-0}" -eq 3 ]; then
  logger -t service-canary "restarting bird-dashboard (user unit)"
  systemctl --user -M vives@ restart bird-dashboard.service 2>/dev/null || \
    su - vives -c "XDG_RUNTIME_DIR=/run/user/1000 systemctl --user restart bird-dashboard"
elif [ "${sn:-0}" -eq 3 ]; then
  logger -t service-canary "restarting ssh.service"
  systemctl restart ssh.service
fi
