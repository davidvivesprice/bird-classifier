# Pi systemd units (source of truth)

Repo copy of everything installed on the Pi (ad-hoc extras on the box —
`bird-demo-loop.service`, a `bird-audio.service.d/` nice-level drop-in —
are not tracked here). User units live at
`~/.config/systemd/user/` on the Pi; the canary + watchdog layer is
SYSTEM-level (root) — see below. Updated 2026-07-17 (journald logging,
alert unit, logrotate, timer fixes).

## Long-running services (user units)

- `bird-pipeline.service` — `bird_pipeline_v3.py` with `PI_MODE=1`, ports 8100/8105, `Restart=always`, `RestartSec=10` (Hailo PCIe driver release window), `LimitCORE=infinity` (F4 SEGV capture)
- `bird-dashboard.service` — uvicorn `dashboard.api:app`, port 8099, `Restart=always`, `RestartSec=5`
- `go2rtc.service` — RTSP-in / WebRTC-out, port 1984, `Restart=always`, `RestartSec=5`
- `cloudflared.service` — Cloudflare tunnel `pi5.vivessato.com` → `:8099`, `Restart=always`, `RestartSec=5`
- `bird-audio.service` — BirdNET audio analyzer (journal logging from day one)

All log to the **journal** (persistent, capped at 500M via
`journald-99-persistent.conf`). Read logs with:

```bash
journalctl --user -u bird-pipeline -S today          # follow: add -f
journalctl --user -u bird-dashboard --grep 'ERROR'
```

The old flat files under `~/logs/` are legacy (one `.1.gz` archive of the
pipeline, dashboard, and go2rtc logs was kept when they were rotated on
2026-07-17; cloudflared's was still under the 20M threshold); `bird-logrotate.timer`
keeps anything still appending there bounded.

## Timer-driven oneshots (user units)

- `bird-integrity-audit.service` + `.timer` — daily 03:40 (+ up to 5 min random delay), `Persistent=true`, runs `Nice=15` / `IOSchedulingClass=idle` so it never competes with live detection
- `refresh-rtsp.service` + `.timer` — daily 03:20 (skewed from the iMac's 03:10), `Persistent=true`; `ExecStartPre` waits up to 60 s for the CloudKey (user-manager `network-online.target` is a stub — nothing populates it, so unit ordering cannot wait for the network)
- `bird-logrotate.service` + `.timer` — daily 03:50, user-space `logrotate` over `~/logs` using `bird-logrotate.conf` (state in `~/.local/state/bird-logrotate.state`)
- `tools/pi5-thermal-watch.service` + `.timer` — every 60 s, appends one row to `~/logs/pi5-thermal-watch.csv` (rotated monthly by bird-logrotate)

Timers deliberately have **no `Requires=`** — that pulled a service start in
at every boot when `timers.target` came up, off-schedule. The
timer→service link is implicit via the unit name (the thermal timer names
its service explicitly via `Unit=`).

## Failure alerting

`bird-alert@.service` + `bird-alert.sh`: the pipeline, dashboard, and all
timer-driven oneshots carry `OnFailure=bird-alert@%N.service` (go2rtc,
cloudflared, and bird-audio don't yet). On failure it appends to
`~/logs/unit-failures.log` and rewrites `~/logs/unit-failure-latest.json`
(machine-readable — dashboard can surface it) and logs via `logger -t bird-alert`.

## Drop-in

`bird-pipeline.service.d/coredump.conf` — `LimitCORE=infinity` (F4). Now
also baked into the unit itself; the drop-in stays so a partial redeploy
can never disarm SEGV capture.

## Install / update (user units)

```bash
ssh vives@pi5.local "mkdir -p ~/.config/systemd/user/bird-pipeline.service.d"
# NOTE: --exclude keeps the SYSTEM canary out of the user manager (its
# remedies need root and would silently fail there).
rsync -av --exclude 'service-canary.*' deploy/systemd/*.service deploy/systemd/*.timer vives@pi5.local:.config/systemd/user/
rsync -av tools/pi5-thermal-watch.service tools/pi5-thermal-watch.timer vives@pi5.local:.config/systemd/user/
rsync -av deploy/systemd/bird-pipeline.service.d/coredump.conf vives@pi5.local:.config/systemd/user/bird-pipeline.service.d/
ssh vives@pi5.local "systemctl --user daemon-reload"
ssh vives@pi5.local "systemctl --user enable --now bird-pipeline bird-dashboard go2rtc cloudflared bird-audio bird-integrity-audit.timer refresh-rtsp.timer bird-logrotate.timer pi5-thermal-watch.timer"
ssh vives@pi5.local "loginctl enable-linger vives"  # services survive logout
```

The units reference `bird-alert.sh` and `bird-logrotate.conf` at their
deployed repo path `~/bird-classifier/deploy/systemd/` — the normal repo
rsync deploy keeps those current.

## System-level self-heal layer (root — separate install)

These are **SYSTEM** units/files, NOT for the user manager:

- `service-canary.service` + `.timer` + `.sh` — every 2 min: dashboard HTTP,
  sshd banner, and a daytime pipeline-wedge check (`frames_processed` frozen
  ~16 min → restart bird-pipeline). Escalation: restart → reboot.
  Install: `service-canary.sh` → `/usr/local/sbin/` (`chmod +x`), units →
  `/etc/systemd/system/`, then `sudo systemctl daemon-reload && sudo systemctl enable --now service-canary.timer`.
- `watchdog.conf` — hardware watchdog config (`/etc/watchdog.conf` additions)
- `wd-disk-canary.sh` — O_DIRECT disk canary used by the watchdog (`/usr/local/sbin/`)
- `journald-99-persistent.conf` — `/etc/systemd/journald.conf.d/99-persistent.conf`
  (`Storage=persistent`, `SystemMaxUse=500M`), then `sudo systemctl restart systemd-journald`

## Verify

```bash
ssh vives@pi5.local "systemctl --user list-timers --all"
ssh vives@pi5.local "systemctl --user is-active bird-pipeline bird-dashboard go2rtc cloudflared bird-audio"
ssh vives@pi5.local "sudo systemctl is-active service-canary.timer"
```

## Hailo restart constraint

**Never `kill -9` `bird-pipeline`.** The Hailo-8L PCIe driver holds the VDevice busy for several seconds after an unclean exit; the next launch hits `HAILO_DEVICE_IN_USE(73)` and crash-loops. Always `systemctl --user restart bird-pipeline` — sends SIGTERM, waits for clean exit, then waits `RestartSec=10` for the driver to release the device before respawning.
