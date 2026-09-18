# Superpowers docs — REDIRECT

> **2026-09-18:** the copies that used to live in this folder are gone. Active scaffolding for the iMac side is `~/docs/bird-observatory/working/`; the pre-split historical archive (plans / specs / progress / reviews, 2026-03 → 2026-04-25) has ONE home: `~/bird-classifier-pi/docs/historical/`. The iMac itself is a frozen reference (ROADMAP 2026-07-03).

## Where things went

| Old repo path | Now |
|---|---|
| `docs/superpowers/{specs,plans,progress}/*.md` (active copies) | `~/docs/bird-observatory/working/{specs,plans,progress}/` — the April 2026 plans, handoffs and the iMac as-built spec sit under `working/historical/` |
| `docs/superpowers/{plans,progress,specs,reviews}/historical/*.md` | `~/bird-classifier-pi/docs/historical/{plans,progress,specs,reviews}/` — same files (byte-identical bar the trailing newline), bannered; the two that existed only here, `2026-04-25-review-ui-shared-helpers.md` and `2026-04-25-evening-handoff.md`, were copied there |
| `docs/superpowers/progress/historical/cross-claude-comms.md` (04-26 snapshot) | a prefix of `~/bird-classifier-pi/docs/historical/progress/cross-claude-comms.md` (full bus, frozen 2026-07-03) |
| `docs/superpowers/specs/historical/2026-04-25-hailo-playbook.md` | `~/bird-classifier-pi/docs/working/specs/2026-04-25-hailo-playbook.md` (still the live reference) |
| `docs/superpowers/progress/2026-04-11-v3-verification/` | still here — `scripts/verify_v3_prototype.py` writes into it; an identical copy is in the Pi archive |

Code comments in this repo (`reviews_db.py`, `pipeline/*.py`, `tests/`) still cite `docs/superpowers/...` paths; read them against the Pi archive. The `bird-integrity-audit` plist already points at `~/docs/`.
