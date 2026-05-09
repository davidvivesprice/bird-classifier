# Pi docs — state of work 2026-04-30

## What's done

- **Doc audit (chapters.jsx):** 44 verified, 5 drift fixed, 1 hallucination removed, 2 smells flagged.
  Full report at `docs/working/progress/DOC_AUDIT_PI_BOOK.md`.
- **Tracker id_switches:** Added counter + hit-counter detection to `pipeline/tracker.py`;
  wired into process_thread.py health update; two tests added.
- **Stale health path fixed:** `shared.tracker.<camera>.id_switches` → `pipeline.feeder.tracker.id_switches`
  corrected in both `chapters.jsx` (book) and `03-pipeline.md` (doc).

## Docs state by file

### Already right (utilitarian, factual)
- `00-overview.md` — mission, what runs, where code lives
- `01-hardware.md` — inventory, boot order, filesystem, camera streams
- `02-services.md` — service table, env file, restart discipline, crash-loop detection
- `05-dashboard.md` — layout, live view tradeoffs, model lab, square crop, URL rewrite
- `06-pi-review.md` — API surface, schema with indexes, what it enables/isn't
- `07-thermal.md` — steady-state numbers, CSV columns, failure modes
- `08-deployment.md` — edit-deploy loop, cold-start runbook, coordination protocol

### Need rewriting (book-chapter prose style, not reference docs)
- `03-pipeline.md` (42 KB) — first ~130 lines are solid; rest is "the frame / three difficulties /
  biggest levers / cutting-edge research validation" book structure. Also has two stale items:
  line 224 says ~167 ms tolerance (should be ~400 ms), line 240 says FrameCapture/resumes at sunrise
  (should be: pipeline pauses, resumes 30 min before sunrise).
- `04-hailo-engine.md` (42 KB) — first ~80 lines solid; rest is same book-chapter pattern.
- `09-the-unified-brain.md` (45 KB) — picture + comparison table are useful; rest is book prose.

## Current task

Rewriting 03, 04, 09 to match the utilitarian style of the small docs:
- Tables and commands for factual content
- Short explanatory prose only where a fact needs context (not "the frame", not "biggest levers")
- No research-validation sections, no non-goals prose, no cross-system narrative
- Keep all the real facts (bench numbers, API calls, error codes, watch-outs)
