# Pi 5 Overnight Build — Live Progress Log

**Started:** 2026-04-24 evening, David went to bed with the instruction "don't stop until everything is done."

This log is append-only. Each entry is timestamped. When I hit a decision, I write it here with rationale. Survives context compression.

---

## What "done" means

The master checklist (from David's messages):

- [ ] Pi 5 runs the observatory (dashboard + pipeline + audio + tunnel)
- [ ] Dashboard reachable at `pi5.vivessato.com`
- [ ] AIY producing real-time labels
- [ ] Model switcher: AIY + 4 Hailo candidates, one-click switch
- [ ] No Coral dependencies
- [ ] Better classifier (intent unclear — interpreting as "smarter defaults, better OOD, better UX around uncertainty")
- [ ] Audio analysis on the dashboard
- [ ] 4 distinct themes (colors + fonts + layouts each unique)
- [ ] Dashboard improvements (general polish)
- [ ] Morning brief document for David

---

## Infrastructure milestones (earlier tonight)

- **NVMe cutover** — 2026-04-24 ~01:18 local. Clone succeeded; SD removed; first boot from NVMe failed. Root cause: Realtek RTL9210 bridge in the Ugreen enclosure needed USB-MSD *first* in BOOT_ORDER. Fixed by changing `0xf461` → `0xf14`. Verified: `findmnt / = /dev/sda2 ext4`.

- **Hailo install + benchmark** — `sudo apt install hailo-all`. Kernel driver `hailo_pci` loaded. `hailortcli fw-control identify` shows Hailo-8L, firmware 4.23. Benchmark on `yolov8s_h8l.hef`: **58.67 FPS streaming, 12.96 ms HW latency**. The flagship reason for Pi 5 in the first place — check.

- **System apt deps** — python3 3.13, ffmpeg, sqlite3, jq, build-essential, libssl-dev, libffi-dev, libjpeg-dev, zlib1g-dev, libbz2-dev, libncurses-dev, libreadline-dev, liblzma-dev, libsqlite3-dev, tk-dev, uuid-dev, pyenv (apt pkg, unused yet), libedgetpu1-std (Coral deps, probably removing after verify).

- **Repo rsync** — `~/bird-classifier/` → `pi5.local:~/bird-classifier/` (2.1 GB, 21s, excluded venvs + dataset dirs + credentials). Git history preserved.

- **venv creation + pip install started** — core deps installing. Background task in flight.

## Decisions log

**Skip Python 3.9 via pyenv.** The old iMac setup used 3.9 because pycoral required ≤3.9. We're Coral-free now, so use system Python 3.13. Saves 20+ minutes of compile time.

**YOLOv8s over YOLOv8n.** The iMac pipeline uses YOLOv8n. Hailo has a pre-compiled `yolov8s_h8l.hef` but no YOLOv8n for 8L. Swapping to "s" is heavier but easily fits (58 FPS vs the pipeline's 5 fps ingest). If quality regresses I'll hunt for a nano compile.

**AIY on CPU, not Hailo.** Hailo DFC only runs on x86_64 and takes hours per compile. Tonight, AIY stays on CPU via `tflite_runtime`. If David later wants AIY on Hailo, that's a day-next task on another machine.

**Rsync, not git clone.** No GitHub auth setup on the Pi. Repo contents (including `.git/`) copied over. Push/pull will work once we set up the Pi's own SSH key on GitHub.

**Defer `~/bird-snapshots/` rsync.** 150 GB. Not needed for MVP. Pi starts with a fresh DB and captures new snapshots.

**Reuse iMac Cloudflare cert for tunnel creation.** Copy `~/.cloudflared/cert.pem` from iMac to Pi — this lets `cloudflared tunnel create` on the Pi without re-OAuthing. No user interaction needed.

## Log entries

### Entry 1 — 2026-04-25 ~early AM: persistent state docs created

Wrote `project_pi5_overnight_build.md` to memory (survives compression) and this progress log. TodoWrite list also populated for in-session tracking. Now executing through the todo list methodically.

---

(more entries appended below as work progresses)
