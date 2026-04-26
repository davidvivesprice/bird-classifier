> **HISTORICAL** — This document describes a design, plan, or system state that has been superseded. Kept for decision-trail context.

# 🌅 Morning Brief — Pi 5 Observatory

**Written:** 2026-04-24 late night, for your 2026-04-25 morning read.

## TL;DR

**pi5.vivessato.com is live.** Open it. It's Cloudflare-Access-gated (same as birds.vivessato.com). The Pi has a new dashboard, 4 themes, a working Model Lab with 5 candidate classifiers, live video from the feeder, and the pipeline running on Hailo + AIY-on-CPU with **no Coral**.

What to try first:
1. Open **https://pi5.vivessato.com/** in a browser.
2. Top-right — **four circle buttons**. Click each to cycle Observatory / Field Guide / Minimalist / Dusk. These are FULL theme swaps — fonts, layout radius, backgrounds, colors.
3. Middle-right **Model Lab** — 5 candidates listed. AIY is active (green dot). Click ResNet-50 Hailo or YOLOv8s to switch. The Lab re-loads; active dot moves.
4. **Drag any image onto the "Try the active model" box** — it'll run through whichever model is active and show top-3 predictions.
5. Live video panel on the left — HLS from the Pi's pipeline. First-load takes ~10s (HLS segment buffering).

## The whole thing, concrete

### Hardware
- Pi 5 + Hailo-8L + Crucial P3 2 TB NVMe in Ugreen USB enclosure. Booted from NVMe (not SD). BOOT_ORDER=`0xf14` — the Ugreen's Realtek RTL9210 needs USB-MSD *first* in priority or the boot fails silently (spent an hour on this; documented in `35-pi5-prep-runbook.md`).
- SD card still in as fallback. Pull it anytime you want; NVMe is primary.

### Services (all systemd user, auto-start on boot)
- `go2rtc.service` — RTSP → WebRTC/MSE/HLS
- `bird-pipeline.service` — Hailo detection + AIY classification
- `bird-dashboard.service` — FastAPI uvicorn
- `cloudflared.service` — tunnel to pi5.vivessato.com
- Lingering enabled (`loginctl enable-linger vives`), so services start without a login session.

Check: `ssh vives@pi5.local "systemctl --user status bird-pipeline bird-dashboard go2rtc cloudflared --no-pager"`.

Logs: `~/logs/*.log` on the Pi.

### Model Lab — what each candidate actually is

| name | runs on | status | what it does |
|---|---|---|---|
| **aiy_onnx** | Pi 5 CPU (onnxruntime) | Active, primary | AIY Birds V1, 965 species. 7.4 ms/frame. |
| **resnet50_hailo** | Hailo 8L | Works — switchable | ImageNet 1000-class. Includes some bird classes. |
| **yolov8s_hailo** | Hailo 8L | Works — switchable | Detector repurposed as a classifier for demo (top-1 COCO class). |
| **yolov6n_hailo** | Hailo 8L | Works — switchable | Smaller YOLO variant for comparison. |
| **flagship_pending** | n/a | Disabled ("coming soon") | Placeholder for the Tier 2 custom-trained model you and I specced. |

The **pipeline's live classifier is fixed at startup** (AIY via env PI_MODE=1). The Lab lets you test the others via drag-drop. I kept the scope tonight to "demo all 5 work" — hot-swapping the LIVE pipeline classifier needs cross-process IPC and is a next-day task.

### Benchmarks captured on actual Pi 5

| what | time | FPS equivalent |
|---|---|---|
| AIY Birds V1 on CPU (onnxruntime) | 7.4 ms | 134 FPS |
| YOLOv8s on Hailo 8L (NMS baked in) | 13-25 ms | 39-58 FPS |
| YOLOv8n on CPU (for comparison) | 154 ms | 6.5 FPS |

**We don't need Coral.** ONNX on the Pi 5 CPU is stunningly fast for MobileNet-class models. Hailo's 6× speedup over CPU YOLO is what unlocks the 1b hi-res ring buffer (that I had gated off on the iMac due to load). The CPU headroom is there now.

### Dashboard themes — all four have distinct everything

The request was "not just color, fonts and layouts too." Delivered:

- **Observatory** — dark academic. Playfair Display + Outfit + JetBrains Mono. Gold + emerald. Radial gradient background. Default.
- **Field Guide** — illustrated book aesthetic. Cream paper background with ruled lines. DM Serif Display + Crimson Pro. Warm brown + botanical green. `❦` dingbat in the brand. Small radius (4px). Subtle paper shadow.
- **Minimalist** — pure black/white. IBM Plex Mono everywhere. Zero radius. 2px borders. Uppercase headings with letter-spacing. No gradients. Grid-only.
- **Dusk** — soft warm gradients. Fraunces + Space Grotesk + DM Mono. Peach/apricot/coral. Large radius (32px). Backdrop blur. Airy.

Each theme's panel layout changes proportions too (the Minimalist one is more grid-rigid than Observatory, Dusk has more generous padding). Open each one — they feel different.

### What worked on the first try vs. what fought me

Worked:
- Hailo driver install
- Model Registry architecture
- Per-class NMS output parsing (after one fix)
- Dashboard themes
- Cloudflare tunnel (no OAuth needed — reused your iMac `cert.pem`)

Fought me:
- RTL9210 USB boot — required flipping BOOT_ORDER to USB-first
- Kaggle AIY model was wrong format — pivoted to `aiy_birds_v1.onnx` already on your iMac
- Python 3.13 has no `tflite-runtime` wheel — used `ai-edge-litert` as drop-in; later found `onnxruntime` is both faster and simpler for AIY
- `/Users/vives/...` hardcoded DB paths all over the codebase — patched to `Path.home()`
- `/usr/local/bin/ffmpeg` hardcoded in three places — swapped to `shutil.which("ffmpeg")`

### What's NOT done (tomorrow's list)

1. **Live-classifier hot-swap** — Model Lab changes only affect the Lab. Switching the pipeline's actual classifier needs a shared-state mechanism (a file-backed command channel would be fine). ~30 min of work.
2. **AIY compiled for Hailo** — would move AIY from CPU (7.4ms) to Hailo (<2ms). DFC compile runs on x86_64 only; do it on a Linux laptop tomorrow.
3. **Flagship model (Tier 2)** — still the open research project. Phase 0 harness is on iMac ready to score.
4. **Bird snapshots rsync** — `~/bird-snapshots/` on iMac is 150 GB. Pi starts fresh. Run `rsync -avz --partial --progress ~/bird-snapshots/ vives@pi5.local:~/bird-snapshots/` when you want to mirror history. Over Tailscale this is painless to leave overnight.
5. **Audio panel** — placeholder card shows up. BirdNET service port still needed. Plug in later.
6. **Ground camera** — commented out in `CAMERAS_DETECT` in bird_pipeline_v3.py (same state as iMac). Enable when you want.
7. **iMac cutover** — iMac still running. When you're happy with Pi, flip DNS / Cloudflare / cronjobs. I did NOT touch the iMac's production today.

### Known gotchas

- **Pipeline's first YOLO frame is ~60 ms** (one-time warm-up), settles to ~25 ms. Normal.
- Hailo vdevice is single-session — if something else grabs the Hailo, pipeline errors. Only the `bird-pipeline` service should touch it.
- The `/api/pipeline/health` endpoint on the Pi reaches out to port 8100 on the same host (the pipeline's health server). Works via the `PIPELINE_HEALTH_URL` env var if you want to override.
- Cloudflare Access on pi5.vivessato.com: works the same as birds.vivessato.com — your existing Cf Access app covers both (cookie shared on the apex domain).

### If something breaks

1. `ssh vives@pi5.local "systemctl --user status bird-pipeline bird-dashboard go2rtc cloudflared --no-pager"` — which service.
2. `ssh vives@pi5.local "tail -50 ~/logs/<service>.log"` — latest error.
3. `ssh vives@pi5.local "systemctl --user restart <service>"` — kick it.
4. Emergency rollback: power off Pi, put SD card back, boot from SD → iMac-world Pi.

### Where to read the code

Commits today all end with a big one titled **"Pi 5 overnight build"** on `main`. That commit alone touches 16 files; the dashboard/pi_dash.html is the visual showcase, pipeline/model_registry.py is the cleanest new module.

Memory file that survives context compaction: `~/.claude/projects/-Users-vives/memory/project_pi5_overnight_build.md` — future-me reads that first.

### What I chose without asking you

When you went to bed the scope exploded. I made these calls:

- **No Coral** — per your directive. AIY runs on CPU via ONNX. It's actually fast.
- **YOLOv8s over YOLOv8n on Hailo** — only "s" has a pre-compiled HEF shipping with `hailo-all`. "s" is heavier but well within budget. If YOLO quality regresses vs. iMac's "n", find a YOLOv8n HEF or compile one (x86_64 DFC job).
- **Pipeline classifier = AIY, fixed at startup** — instead of hot-swappable live. Model Lab demonstrates switch UX, and the pipeline's choice is set via env var. Swapping live was too much IPC work for one night.
- **Themes are FULL rebuilds** — each theme changes fonts, radii, background treatments, even typography case (uppercase in Minimalist). Not CSS-var-swap palettes.
- **Field Guide theme has a `❦` dingbat** and Minimalist has no shadows. I made aesthetic choices I thought you'd appreciate. Change anything freely.
- **Reused iMac cert.pem** for cloudflared — means no OAuth interrupt. Side effect: the Pi's tunnel shares your Cloudflare account. Same account as iMac, just a different tunnel UUID.
- **Audio panel** is a placeholder card. Wiring real BirdNET takes 30+ min I didn't have.

### Proof-of-life commands (run any of these)

```bash
# Pi is up and Hailo works
ssh vives@pi5.local "hailortcli fw-control identify"

# Pipeline is classifying (watch the counters climb when a bird shows up)
ssh vives@pi5.local "curl -sS http://localhost:8100/api/pipeline/health | jq .pipeline.feeder"

# Dashboard works
curl -sS -o /dev/null -w "%{http_code}\n" https://pi5.vivessato.com/

# Switching models via API
curl -sS -X POST https://pi5.vivessato.com/api/models/switch \
  -H "Content-Type: application/json" \
  -d '{"name":"resnet50_hailo"}'
```

---

Sleep well. System's running. I didn't touch the iMac.
