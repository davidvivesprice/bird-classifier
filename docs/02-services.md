# 02 · Services

Four systemd-user services hold the system up, plus a thermal-watch timer. Everything starts without a login session because `loginctl enable-linger vives` is set.

## The four services

| Unit | Purpose | Listens | Restart |
|---|---|---|---|
| `go2rtc.service` | RTSP-in (UniFi) → WebRTC / MSE / HLS-out | `:1984` | `systemctl --user restart go2rtc` |
| `bird-pipeline.service` | `python3 -u bird_pipeline_v3.py` with `PI_MODE=1` — the detection pipeline (see `03-pipeline.md`) | `:8100` health, `:8105` SSE | `systemctl --user restart bird-pipeline` |
| `bird-dashboard.service` | uvicorn `dashboard.api:app` — the Pi-native dashboard (see `05-dashboard.md`) | `:8099` | `systemctl --user restart bird-dashboard` |
| `cloudflared.service` | Cloudflare tunnel UUID `bf725288-989b-4ae4-9d71-ea457310a8d4` (config at `~/.cloudflared/config.yml`) → `pi5.vivessato.com` | — | `systemctl --user restart cloudflared` |

Health-check one-liner:

```bash
ssh vives@pi5.local "systemctl --user is-active bird-pipeline bird-dashboard go2rtc cloudflared"
```

All four return `active` when healthy.

## Pipeline env file

`bird-pipeline.service` sources `~/.bird-observatory-env` (in addition to its `Environment=` directives in the unit). The env file holds:

```
UNIFI_API_KEY=...
PIPELINE_HIRES_RING=authoritative
PI_CLASSIFIER=aiy_onnx
```

The unit itself sets:

- `PI_MODE=1` — gates Pi-only code paths (registry-based classifier, Hailo detector, hi-res ring as authoritative, etc.)
- `PIPELINE_HEALTH_PORT=8100`
- `PIPELINE_SSE_PORT=8105`

`PI_CLASSIFIER` controls which entry from `pipeline/model_registry.py` is the active classifier at startup. The dashboard's "switch" affordance writes this file then triggers `systemctl --user restart bird-pipeline` — see `06-pi-review.md` for the per-model accuracy story that hangs off it.

## Logs

`~/logs/<service>.log` per service (NOT `~/bird-snapshots/logs/` — that's for DBs and pipeline event data). Tail tools:

```bash
ssh vives@pi5.local "tail -50 ~/logs/bird-pipeline.log"
ssh vives@pi5.local "journalctl --user -u bird-dashboard -n 100 --no-pager"
```

## Thermal watch (timer + service, every 60 s)

Two extra units in `~/.config/systemd/user/`:

- `pi5-thermal-watch.service` — single-shot Python sampler at `/home/vives/bird-classifier/tools/pi5_thermal_watch.py`.
- `pi5-thermal-watch.timer` — fires every 60 s with `OnUnitActiveSec=1min`, accuracy 5 s.

Each fire appends one CSV row to `~/logs/pi5-thermal-watch.csv` with CPU temp, ARM clock, fan RPM, Hailo NPU temp (best-effort via `hailortcli sensors`), and pipeline counters. Goal: 24 h baseline so we know whether the 83-85 °C steady-state holds. See `07-thermal.md`.

## Restarting from CLI

The dashboard's `/api/models/switch` writes the env file then does a non-blocking `systemctl --user restart bird-pipeline`. Manual restarts go through the same systemctl path. **Do not** `kill -9` Hailo-using processes mid-inference — the PCIe driver holds the device "busy" for a few seconds after an unclean shutdown. Use `systemctl --user restart` (graceful).

If the bird-pipeline service crash-loops (>3 crashes in quick succession), systemd may rate-limit. Check with:

```bash
systemctl --user status bird-pipeline -n 50
```

`Restart=always` with `RestartSec=10` is the policy.

## What is NOT a service on the Pi

- No `audio_analyzer` / `enhanced_audio` services. The audio panel on the dashboard is currently a placeholder; BirdNET integration is a future task (see `historical/plans/2026-03-23-audio-accuracy-plan.md` for the iMac-era design that we'll port).
- No `bird-integrity-audit`. The iMac runs that as a LaunchAgent on schedule; the Pi side hasn't ported it.
- No RTSP-token refresh cron. The iMac-side `refresh_rtsp.py` runs daily at 3:10 AM; on the Pi the UNIFI_API_KEY is stable and the RTSP URL doesn't carry an expiring token.
