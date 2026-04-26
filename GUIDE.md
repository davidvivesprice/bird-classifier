# Bird Observatory — Pi-side Guide

This is the Pi-side repo. It runs the bird observatory on a Raspberry Pi 5 with a Hailo-8L AI Hat. The current dashboard is at https://pi5.vivessato.com/ (and on-LAN at http://pi5.local:8099/).

## The book

The reference book lives at **`~/docs/bird-observatory-pi/`** — chapters 00 through 08 plus a chapter-index README. Start there:

- [`~/docs/bird-observatory-pi/README.md`](file:///Users/vives/docs/bird-observatory-pi/README.md) — chapter index
- [`~/docs/bird-observatory-pi/00-overview.md`](file:///Users/vives/docs/bird-observatory-pi/00-overview.md) — what the Pi observatory is, mission, where the code lives

The book is parallel to iMac's reference at `~/docs/bird-observatory/`.

## In-repo docs

What lives **inside this repo** under `docs/`:

- [`docs/working/specs/2026-04-25-hailo-playbook.md`](docs/working/specs/2026-04-25-hailo-playbook.md) — deep Hailo-8L API + scheduler + DFC compilation reference (the canonical pairing with chapter `04-hailo-engine.md`)
- [`docs/working/progress/2026-04-25-pi5-handoff.md`](docs/working/progress/2026-04-25-pi5-handoff.md) — most recent end-of-session handoff
- [`docs/working/progress/2026-04-25-pi-repo-split.md`](docs/working/progress/2026-04-25-pi-repo-split.md) — why the iMac and Pi repos split
- [`docs/working/progress/cross-claude-comms.md`](docs/working/progress/cross-claude-comms.md) — cross-Claude message bus
- [`docs/historical/`](docs/historical/) — 60+ banner'd retired plans / specs / progress / reviews

Project mission, principles, and Pi-architecture summary are in [`CLAUDE.md`](CLAUDE.md). Full audit report at [`DOC_AUDIT.md`](DOC_AUDIT.md).
