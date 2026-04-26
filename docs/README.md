# `docs/` (in-repo)

The reference **book** for the Pi-side observatory lives outside this repo, at `~/docs/bird-observatory-pi/` — chapters 00 through 08 plus that book's own README. See [`~/docs/bird-observatory-pi/README.md`](file:///Users/vives/docs/bird-observatory-pi/README.md).

What lives **inside this repo** under `docs/`:

- `working/` — active engineering artifacts cross-referenced by the book chapters:
  - `specs/2026-04-25-hailo-playbook.md` — deep Hailo-8L API/scheduler/DFC reference (the canonical pairing for chapter 04)
  - `progress/2026-04-25-pi5-handoff.md` — most recent end-of-session handoff
  - `progress/2026-04-25-pi-repo-split.md` — why the iMac and Pi repos split
  - `progress/cross-claude-comms.md` — cross-Claude message bus (append-only; David relays)
- `historical/` — superseded plans, specs, progress logs, and reviews from before the 2026-04-25 iMac/Pi repo split. Each file carries a `> **HISTORICAL**` banner. Not part of the book; kept for decision-trail context.

The audit summary is at the repo root: [`../DOC_AUDIT.md`](../DOC_AUDIT.md).
