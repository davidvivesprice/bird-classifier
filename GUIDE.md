# Bird Observatory — Pi-side Guide

This is the Pi-side repo. It runs the bird observatory on a Raspberry Pi 5 with a Hailo-8L AI Hat. The current dashboard is at https://pi5.vivessato.com/ (and on-LAN at http://pi5.local:8099/).

The full reference lives in [`docs/`](docs/). Chapter index:

- [`docs/00-overview.md`](docs/00-overview.md) — what the system is, mission, where the code lives
- [`docs/01-hardware.md`](docs/01-hardware.md) — Pi 5 + Hailo-8L + NVMe + camera + filesystem layout
- [`docs/02-services.md`](docs/02-services.md) — the four systemd-user services + thermal-watch timer
- [`docs/03-pipeline.md`](docs/03-pipeline.md) — frame capture → motion gate → Hailo YOLO → tracker → AIY → snapshot writer
- [`docs/04-hailo-engine.md`](docs/04-hailo-engine.md) — multi-model on one VDevice (Path 1)
- [`docs/05-dashboard.md`](docs/05-dashboard.md) — Pi-native dashboard, Live view, Model Lab, themes, info modal
- [`docs/06-pi-review.md`](docs/06-pi-review.md) — yes/no review API + UI, per-classifier accuracy
- [`docs/07-thermal.md`](docs/07-thermal.md) — observed thermal envelope + the watch tool
- [`docs/08-deployment.md`](docs/08-deployment.md) — repo split workflow, rsync, runbook, don't-do list

Working reference (deeper but not chapter-shaped):

- [`docs/working/specs/2026-04-25-hailo-playbook.md`](docs/working/specs/2026-04-25-hailo-playbook.md) — the deep Hailo-8L API + scheduler + DFC reference
- [`docs/working/progress/2026-04-25-pi5-handoff.md`](docs/working/progress/2026-04-25-pi5-handoff.md) — most recent end-of-session handoff
- [`docs/working/progress/2026-04-25-pi-repo-split.md`](docs/working/progress/2026-04-25-pi-repo-split.md) — why the iMac and Pi repos split
- [`docs/working/progress/cross-claude-comms.md`](docs/working/progress/cross-claude-comms.md) — cross-Claude message bus

Historical (pre-Pi or superseded designs / plans / progress) lives under [`docs/historical/`](docs/historical/) with a `> **HISTORICAL**` banner on each file.

Project mission, principles, and Pi-architecture overview are in [`CLAUDE.md`](CLAUDE.md).
