# Pi systemd-user units (source of truth)

Snapshot taken from the live Pi at `~/.config/systemd/user/` on 2026-04-30.

## Long-running services

- `bird-pipeline.service` — `bird_pipeline_v3.py` with `PI_MODE=1`, ports 8100/8105, `Restart=always`, `RestartSec=10` (Hailo PCIe driver release window)
- `bird-dashboard.service` — uvicorn `dashboard.api:app`, port 8099, `Restart=always`, `RestartSec=5`
- `go2rtc.service` — RTSP-in / WebRTC-out, port 1984, `Restart=always`, `RestartSec=5`
- `cloudflared.service` — Cloudflare tunnel `pi5.vivessato.com` → `:8099`, `Restart=always`, `RestartSec=5`

## Timer-driven oneshots

- `bird-integrity-audit.service` + `.timer` — hourly, `OnCalendar=hourly`, `RandomizedDelaySec=300`, `Persistent=true`
- `refresh-rtsp.service` + `.timer` — daily 03:10, `OnCalendar=*-*-* 03:10:00`, `Persistent=true`

## Thermal watch (already in `tools/`)

- `tools/pi5-thermal-watch.service` + `tools/pi5-thermal-watch.timer` — every 60 s, appends one row to `~/logs/pi5-thermal-watch.csv`

## Install

```bash
ssh vives@pi5.local "mkdir -p ~/.config/systemd/user/"
rsync -av deploy/systemd/*.service deploy/systemd/*.timer vives@pi5.local:.config/systemd/user/
ssh vives@pi5.local "systemctl --user daemon-reload"
ssh vives@pi5.local "systemctl --user enable --now bird-pipeline bird-dashboard go2rtc cloudflared bird-integrity-audit.timer refresh-rtsp.timer pi5-thermal-watch.timer"
ssh vives@pi5.local "loginctl enable-linger vives"  # services survive logout
```

## Verify

```bash
ssh vives@pi5.local "systemctl --user list-timers --all"
ssh vives@pi5.local "systemctl --user is-active bird-pipeline bird-dashboard go2rtc cloudflared"
```

All four services should report `active`. All three timers should show `NEXT` populated.

## Hailo restart constraint

**Never `kill -9` `bird-pipeline`.** The Hailo-8L PCIe driver holds the VDevice busy for several seconds after an unclean exit; the next launch hits `HAILO_DEVICE_IN_USE(73)` and crash-loops. Always `systemctl --user restart bird-pipeline` — sends SIGTERM, waits for clean exit, then waits `RestartSec=10` for the driver to release the device before respawning.
