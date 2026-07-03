# `docs/` (in-repo)

The reference **book** for the Pi-side observatory lives outside this repo, at `~/docs/bird-observatory-pi/` — chapters 00 through 10 plus that book's own README. See [`~/docs/bird-observatory-pi/README.md`](file:///Users/vives/docs/bird-observatory-pi/README.md).

What lives **inside this repo** under `docs/`:

- `working/` — active engineering artifacts cross-referenced by the book chapters:
  - `specs/2026-04-25-hailo-playbook.md` — deep Hailo-8L API/scheduler/DFC reference (the canonical pairing for chapter 04)
  - `specs/2026-07-02-overlay-video-clock-sync-design.md` — live-overlay video-clock sync design (pairs with chapter 10)
  - `progress/2026-07-03-native-crash-isolation.md` — the two-crasher SEGV story + decode/detect child-process cage
  - `progress/2026-04-25-pi5-handoff.md` — the Pi bring-up handoff (newer progress notes sit alongside it)
  - `progress/2026-04-25-pi-repo-split.md` — why the iMac and Pi repos split
  - `progress/cross-claude-comms.md` — cross-Claude message bus (historical; one coder owns both sides now)
- `historical/` — superseded plans, specs, progress logs, and reviews from before the 2026-04-25 iMac/Pi repo split. Each file carries a `> **HISTORICAL**` banner. Not part of the book; kept for decision-trail context.

The audit summary is at the repo root: [`../DOC_AUDIT.md`](../DOC_AUDIT.md).
