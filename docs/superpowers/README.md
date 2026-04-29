# Superpowers docs — REDIRECT

> **As of 2026-04-26, this content has consolidated to `~/docs/bird-observatory/working/`.**
>
> The numbered chapters in `~/docs/bird-observatory/` are the canonical reference book; `working/` holds the active scaffolding (specs, plans, progress notes). Edit there, not here.

For now, the files in this folder still exist — they were copied (not moved) on 2026-04-26 to avoid breaking external references (LaunchAgent plists, code comments). If you find a copy in this folder and a copy under `~/docs/bird-observatory/working/` that disagree, the **`~/docs/`** version wins.

## Mapping

| Repo path | New canonical location |
|-----------|----------------------|
| `docs/superpowers/specs/*.md` | `~/docs/bird-observatory/working/specs/` |
| `docs/superpowers/plans/*.md` | `~/docs/bird-observatory/working/plans/` |
| `docs/superpowers/progress/*.md` | `~/docs/bird-observatory/working/progress/` |
| `docs/superpowers/specs/historical/`, etc. | `~/docs/bird-observatory/working/historical/` (empty for now; will populate as in-flight items ship) |

## Why both copies for now?

- LaunchAgent plists still reference `docs/superpowers/plans/2026-04-22-data-integrity-audit.md` and similar
- Code comments may reference repo paths
- Dashboard `/api/docs/{path}` serves from this tree

These will be migrated in a follow-up pass. Until then: **edit the `~/docs/` copy; the repo copy is read-only**.
