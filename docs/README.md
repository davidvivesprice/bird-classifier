# Pi-side observatory docs

This is the reference for the Raspberry Pi 5 + Hailo-8L observatory at `pi5.vivessato.com`. The Pi node went live on 2026-04-25; the iMac side runs in parallel from a separate repo (`/Users/vives/bird-classifier/`).

## Chapters

Read top-down for the system tour; jump to a chapter for a specific layer.

| | |
|---|---|
| [00 · Overview](00-overview.md) | What the Pi observatory is, mission, where the code lives |
| [01 · Hardware](01-hardware.md) | Pi 5, Hailo-8L AI Hat, NVMe, camera, BOOT_ORDER, filesystem layout |
| [02 · Services](02-services.md) | The 4 systemd-user services + thermal-watch timer |
| [03 · Pipeline](03-pipeline.md) | Frame capture → motion gate → Hailo YOLO → tracker → AIY → snapshot writer |
| [04 · Hailo Engine](04-hailo-engine.md) | Multi-model on one VDevice (Path 1 architecture) |
| [05 · Dashboard](05-dashboard.md) | Pi-native dashboard, Live view, Model Lab, themes, info modal |
| [06 · Pi-Review](06-pi-review.md) | Yes/no review API + UI, per-classifier accuracy |
| [07 · Thermal](07-thermal.md) | Observed thermal envelope + the watch tool |
| [08 · Deployment](08-deployment.md) | Repo split workflow, rsync, runbook, don't-do list |

## Working reference (deeper but not chapter-shaped)

| Path | Purpose |
|---|---|
| [`working/specs/2026-04-25-hailo-playbook.md`](working/specs/2026-04-25-hailo-playbook.md) | Deep Hailo-8L API + scheduler + DFC compilation reference (14 sections) |
| [`working/progress/2026-04-25-pi5-handoff.md`](working/progress/2026-04-25-pi5-handoff.md) | Most recent end-of-session handoff for the next Claude |
| [`working/progress/2026-04-25-pi-repo-split.md`](working/progress/2026-04-25-pi-repo-split.md) | Why the iMac and Pi repos split + the new workflow |
| [`working/progress/cross-claude-comms.md`](working/progress/cross-claude-comms.md) | Cross-Claude message bus (David relays) |

## Historical

Everything pre-2026-04-25 — old plans, design specs, progress logs, retrospective reviews — lives under [`historical/`](historical/), each file carrying a `> **HISTORICAL**` banner. Most are inherited from before the iMac/Pi repo split (carried over via `cp -a`); kept for decision-trail context.

| Bucket | Count |
|---|---|
| `historical/specs/` | 26 (mostly iMac-era design specs) |
| `historical/plans/` | 20 (mostly iMac-era plans) |
| `historical/progress/` | 13 (plus a `2026-04-11-v3-verification/` artifact dir of screenshots + JSON) |
| `historical/reviews/` | 1 (the live-detection-v2 review) |

## Audit

The reference chapters above were authored as part of the 2026-04-26 doc audit pass. Each substantive claim was verified against the live code and runtime state on `pi5.local`. The audit summary is in [`../DOC_AUDIT.md`](../DOC_AUDIT.md) at the repo root.

## Cross-references

- Mission, principles, Pi-architecture summary: [`../CLAUDE.md`](../CLAUDE.md)
- Quick guide / index of this docs/ tree: [`../GUIDE.md`](../GUIDE.md)
- iMac-side observatory reference (separate repo): `/Users/vives/bird-classifier/docs/`
- iMac-side migration head-start docs (kept on the iMac side): `~/docs/bird-observatory/historical/34-pi5-migration.md` and `35-pi5-prep-runbook.md`
