# 00 · Overview — what the Pi 5 observatory is

The bird observatory's primary node is a Raspberry Pi 5 + Hailo-8L AI Hat running Raspberry Pi OS Lite. It pulls RTSP from a UniFi G3 Dome trained on a feeder, runs Hailo-accelerated YOLO + AIY species classification on every frame that passes a motion gate, and serves a live dashboard at `pi5.vivessato.com`.

This Pi node went live 2026-04-25. Before that the observatory ran on a 2017 iMac (Sonoma, OCLP-patched). The migration is documented in `historical/specs/2026-04-25-imac-live-classify-as-built.md` (the iMac as-built that the Pi mirrors-and-diverges from) and in `~/docs/bird-observatory/historical/34-pi5-migration.md` (the why of the hardware move).

## Mission (verbatim from `CLAUDE.md`)

Build a bird identification system that is **delightful to use, deadly accurate, and tells beautiful stories with data**.

- **Casual curious observers**: "What bird is that?" → instant, visual, fun answer.
- **Obsessive birders**: deep data, trends, rare species alerts, seasonal patterns.
- **The system itself**: data feeds back to make identification more accurate over time.

What matters, in order: accuracy, experience, reliability, rich data.

## How the Pi is the right machine for this

Three hardware bets:

1. **Hailo-8L on PCIe** — YOLOv8s detector at ~17 ms / frame (~58 FPS isolated; ~22 ms / 45 FPS when co-scheduled with a Hailo classifier). On the iMac the same model on CoreML was ~98 ms. The detector stops being the per-frame bottleneck.
2. **NVMe on USB-3** — ~450 MB/s sustained. Plenty of headroom for the workload (a few JPEGs/s + SQLite WAL). Not the bottleneck.
3. **No Coral, no second box** — the Pi 5's CPU runs AIY's 965-class bird classifier as ONNX in 7.4 ms / crop, freeing the Hailo NPU for detection plus a future flagship classifier on the same chip via the HailoRT scheduler (see `04-hailo-engine.md`).

## What runs on the Pi (one-line each)

| Surface | What | Where |
|---|---|---|
| RTSP relay | go2rtc multiplexes feeder-main / feeder-sub for downstream consumers | `:1984` |
| Detection pipeline | substream → motion gate → Hailo YOLO → tracker → AIY classifier → snapshot writer → DB | `:8100` health, `:8105` SSE |
| Dashboard | FastAPI/uvicorn — Live view, Recent strip, Model Lab, Pi-review, themes | `:8099` |
| Tunnel | Cloudflared exposes `pi5.vivessato.com` (gated) and `go2rtc.vivessato.com` | — |

## Where the code lives

Two repos, one shared lineage up to commit `5773551` (split point, 2026-04-25):

| | Path | Purpose |
|---|---|---|
| Pi-side, edited on iMac | `/Users/vives/bird-classifier-pi/` | Pi-Claude's home. This is where Pi commits land. |
| Pi-side, runtime | `vives@pi5.local:/home/vives/bird-classifier/` | rsync target. Where the services actually run. |
| iMac-side | `/Users/vives/bird-classifier/` | iMac-Claude's home; iMac dashboard runs from here. Pi-Claude does not push to it. |

See `08-deployment.md` for the rsync workflow and the post-split etiquette (one specifically: cross-platform fixes flow via `working/progress/cross-claude-comms.md`, not silent shared-file edits).

## What this docs/ tree looks like

- `00-overview.md` — this file.
- `01-hardware.md` — Pi 5, Hailo-8L, NVMe, camera, BOOT_ORDER, thermal envelope.
- `02-services.md` — the 4 systemd-user services that hold the system up.
- `03-pipeline.md` — frame capture → motion gate → Hailo YOLO → tracker → AIY → snapshot writer → DB, end to end.
- `04-hailo-engine.md` — multi-model on one VDevice (the Path 1 architecture).
- `05-dashboard.md` — Pi-native dashboard surface (Live view, Model Lab, Recent strip, themes, info modal).
- `06-pi-review.md` — the yes/no review API + UI; per-classifier accuracy.
- `07-thermal.md` — observed thermal regime + the watch tool.
- `08-deployment.md` — repo split workflow, runbook, gotchas.
- `working/` — active reference docs (Hailo playbook, current handoff, repo-split context, comms file).
- `historical/` — superseded plans / specs / progress, kept for decision-trail context. Each carries a `> **HISTORICAL**` banner.

For a single-page rev-1 walkthrough of how an arbitrary frame becomes a labeled snapshot, jump to `03-pipeline.md`.
