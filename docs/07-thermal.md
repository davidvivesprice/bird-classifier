# 07 · Thermal

The Pi 5 + Hailo-8L + active fan is engineered to handle our workload, but it sits near the soft-throttle threshold. This chapter is what we know about the thermal envelope and the watch tool that's accumulating data on it.

## Soft-throttle threshold

The Pi 5's CPU starts soft-throttling at 80 °C. The Hailo-8L has its own thermal envelope (different sensor, different threshold). With the active fan present and at full RPM, the chip handles short excursions to ~85 °C without dropping clock.

## Observed steady-state

Under sustained pipeline load (substream ingest at native ~30 fps, hi-res ring buffer at 5 fps, Hailo YOLO every motion-gate-passing frame, AIY ONNX on every locked track):

- CPU temp: typically **83 – 85 °C**
- Fan: full RPM (~6400 RPM)
- ARM clock: 1.5 GHz nominal, no measurable throttle so far
- Hailo NPU: not consistently sampled (see `pi5_thermal_watch.py` — `hailortcli sensors` output isn't stable across HailoRT versions; the column is best-effort).

These numbers are from the period after the snapshot writer flipped to `PIPELINE_HIRES_RING=authoritative` on 2026-04-25. Thermal watch is collecting per-minute samples to confirm the steady-state holds over a 24 h+ window.

## The watch tool

Two systemd-user units in `~/.config/systemd/user/`:

- `pi5-thermal-watch.service` — `Type=oneshot`, runs `~/bird-classifier/venv/bin/python3 /home/vives/bird-classifier/tools/pi5_thermal_watch.py`. Niced to 10, best-effort I/O class.
- `pi5-thermal-watch.timer` — `OnBootSec=2min`, `OnUnitActiveSec=1min`, accuracy 5 s. Fires roughly every 60 s after a 2-min boot delay.

Each fire appends one CSV row to `~/logs/pi5-thermal-watch.csv` with these columns:

```
ts, cpu_temp_c, arm_clock_hz, hailo_temp_c, fan_rpm,
frames_processed, frames_dropped_oldest, ffmpeg_restarts_lasthr,
yolo_ms_avg, detections_total, active_tracks,
snap_submitted, snap_written, ring_pick_ok, ring_pick_empty,
uptime_s
```

So the row tells you both how hot the chip is AND what the pipeline was doing at that moment, in one view.

## Inspect the data

```bash
ssh vives@pi5.local "tail -10 ~/logs/pi5-thermal-watch.csv | column -t -s,"
```

For a quick aggregate:

```bash
ssh vives@pi5.local 'awk -F, "NR>1 {sum+=\$2; count++} END {print \"avg cpu temp:\", sum/count}" ~/logs/pi5-thermal-watch.csv'
```

## Why this matters going forward

Two reasons we're tracking it:

1. **Multi-model on Hailo will add load.** Today only the YOLOv8s detector runs on the NPU; AIY runs on the CPU. When the Tier 2 flagship classifier ships and runs on the same Hailo VDevice via the scheduler (see `04-hailo-engine.md`), we expect the NPU sensor reading to climb and the per-frame budget to get marginally tighter. The CSV will show whether that pushes CPU or NPU into throttle.
2. **Ring-buffer fps is a knob.** The hi-res ring (`pipeline.hires_ring.HiResCapture`) currently decodes the main stream at 5 fps via `-vf scale=1920:1080,fps=5`. If thermal data shows the chip approaching throttle, dropping ring fps to 3 reduces decode load. This is the easiest single lever.

## Failure modes worth watching for

- **Fan failure** — the chip would climb fast. CSV row would show `fan_rpm=null` (or 0). Soft-throttle at 80 °C; hard limit at 85 °C; chip will eventually shut down to protect itself.
- **Sustained > 85 °C** — clocks down. Per-frame YOLO time creeps up. `yolo_ms_avg` in `/api/pipeline/health` would jump from ~22 ms to ~30+ ms; the CSV will show the correlation.
- **Ambient heat** — summer afternoon spikes. Mitigations: enclose the Pi in something that vents, or move it.

## What this chapter does NOT do

- Doesn't claim a "safe operating envelope." We're tracking, not certifying.
- Doesn't prescribe a thermal-driven action — the system is currently sitting in the comfortable-but-warm zone, not throttling. If the watch shows trending upward, we'll act then.
