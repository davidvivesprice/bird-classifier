# 01 · Hardware

The whole observatory runs on one Pi 5 + Hailo-8L combination plus a USB NVMe and a UniFi camera reachable over the LAN.

## Inventory

| Component | Spec | Note |
|---|---|---|
| Compute | Raspberry Pi 5 (4 GB or 8 GB — `cat /proc/meminfo` to check at runtime) | Verified 4 GB at last boot (`MemTotal: 4146880 kB`). |
| Accelerator | Hailo-8L AI Kit (M.2 module + M.2-to-PCIe HAT) | 13 TOPS INT8, single VDevice slot (see `04-hailo-engine.md`). |
| Storage | Crucial P3 2 TB NVMe in a Ugreen USB-3 enclosure (Realtek RTL9210 bridge) | `/` lives here. SD card stays inserted as fallback. |
| PSU | Official Pi 5 27 W (5 V / 5 A) | Mandatory for Hailo + NVMe co-existence. |
| Cooling | Active pwm-fan | At ~6400 RPM under sustained pipeline load. |
| Camera | UniFi G3 Dome on PoE | RTSP via Cloudkey → go2rtc → pipeline. Same camera the iMac used. |
| OS | Raspberry Pi OS Lite (Debian Trixie, Python 3.13.5) | 64-bit, kernel 6.12.x. |
| HailoRT | 4.19+ via the apt `hailo-all` meta-package | Includes `hailo_pci` driver, firmware, runtime, Python bindings (`hailo_platform`), GStreamer plugins. |

## Boot order

The Pi 5 boots from the USB NVMe first, then falls through:

```
BOOT_ORDER = 0xf14
```

(USB-MSD first; this is the order required for the Realtek RTL9210 USB-NVMe bridge per the Pi 5 EEPROM bootloader docs.) Verify after any EEPROM update with `vcgencmd bootloader_config | grep BOOT_ORDER`.

## What's NOT on the Pi

- **No Coral USB.** Pi-only species classification runs on the CPU via ONNX (AIY Birds V1). The pipeline modules know this — `pipeline.classifier` is lazy-imported and skipped on Pi (PI_MODE=1 wires `pipeline.pi_classifier.PiClassifier` instead).
- **No NAS, no external DB.** SQLite WAL on the NVMe is the sole data store: `~/bird-snapshots/logs/classifications.db`, `pipeline.db`, `pi_reviews.db`.

## SSH + sudo

- `ssh vives@pi5.local` — key auth only, no password (key is the iMac's `~/.ssh/id_ed25519`).
- Sudo is passwordless via `/etc/sudoers.d/vives`.
- mDNS resolves `pi5.local` on the LAN; off-LAN access goes through the Cloudflared tunnel (see `02-services.md`).

## Filesystem layout (Pi-side)

| Path | What |
|---|---|
| `/home/vives/bird-classifier/` | the runtime tree (rsynced from iMac at `/Users/vives/bird-classifier-pi/`). |
| `~/.bird-observatory-env` | pipeline env file: `UNIFI_API_KEY`, `PIPELINE_HIRES_RING`, `PI_CLASSIFIER`. |
| `~/.config/systemd/user/` | the 4 user-service unit files. |
| `~/bird-snapshots/classified/{species}/feeder_*.jpg` | hi-res 1920×1080 JPGs (since the snapshot-writer flip on 2026-04-25). |
| `~/bird-snapshots/annotated/feeder_*.jpg` | the same with corner-bracket overlays drawn in. |
| `~/bird-snapshots/hls/feeder/seg_*.ts` + `live.m3u8` + `segments.json` | HLS recorder output (currently unused by the live view, which uses go2rtc WebRTC directly). |
| `~/bird-snapshots/logs/classifications.db` | per-classification rows. |
| `~/bird-snapshots/logs/pipeline.db` | event-store for the v3 pipeline. |
| `~/bird-snapshots/logs/pi_reviews.db` | Pi-native ✓/✗ verdicts. |
| `~/logs/bird-pipeline.log`, `bird-dashboard.log`, `pi5-thermal-watch.csv` | service stdout + thermal samples. |

## Camera — feeder feeds, two flavors

go2rtc multiplexes one camera into two named streams the pipeline cares about:

| Stream | Resolution | Used by |
|---|---|---|
| `feeder-sub` | 640×360, native ~30 fps | pipeline `FrameCapture` (substream pipe-drain at YOLO rate). |
| `feeder-main` | 1920×1080, native ~30 fps | hi-res ring buffer + HLS recorder + browser WebRTC. |

The pipeline's frame-capture deliberately omits any `-vf fps=N` filter so frames hit Python as fast as YOLO can consume them — adding fps-pacing in ffmpeg adds wall-clock latency between camera capture and pipe-read. (The ring buffer DOES use fps=5 for cost reasons; ring frames are wall-time-stamped at pipe-read, so the pacing latency is contained inside the ring's own clock.)

## Power + thermal

The 27 W PSU comfortably covers Pi 5 + Hailo + NVMe under sustained load. Thermal envelope under typical workload is described in `07-thermal.md` — short version: ~83-85 °C with the fan at full RPM, soft-throttle threshold is 80 °C but the chip handles small excursions without dropping clock.
