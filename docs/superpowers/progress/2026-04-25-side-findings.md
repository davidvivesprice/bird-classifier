# Side findings — orphan ledger

**Purpose:** observations noticed during other work that aren't directly in scope for the immediate task. Append-only. Periodically triage: graduate to a real ticket / fix-now / drop.

Format per entry:
```
## YYYY-MM-DD HH:MM — short title
**Discovered while:** what we were actually doing
**Observation:** what we noticed
**Why it matters / doesn't:** triage notes
**Status:** open | scheduled | rejected | folded into <X>
```

---

## 2026-04-25 ~10:55 — `review_history` legacy backfill missing on iMac

**Discovered while:** restarting iMac dashboard for the review UI bug fix.
**Observation:** The 1,827 historical reviews in the iMac `reviews` table are NOT backfilled into the new `review_history` table. The new table was created on first `get_conn(readonly=False)` after the new code loaded, but it starts empty. Every review going forward gets a history row; everything from before today doesn't.
**Why it matters:** If we want a true audit trail extending back over the dataset Tier 2 will train on, we need a one-time migration that synthesizes one history row per existing reviews row (preserving `verdict`, `correct_species`, `bird_index`, `missed_birds`, `timestamp`, `reviewer`). Doesn't block cleaning data — David's NEW reviews land correctly. Does affect undo coverage on old rows.
**Status:** open. Probably 30 minutes of work; one-shot SQL.

## 2026-04-25 ~11:00 — debug test row in `review_history`

**Discovered while:** end-to-end verifying review2 endpoint after dashboard restart.
**Observation:** I wrote one test row to the iMac `review_history`:
- `id=1`, `file=feeder_2026-04-25_10-55-32_5565.jpg`, `verdict=correct`, `client_id=debug-restart-test-002`, `reviewer=dashboard`
**Why it matters:** Harmless — it's a real verdict on a real file (David could have reviewed it). But it's labeled with a debug client_id and wasn't done by a human. Could keep, could delete, could rewrite the client_id.
**Status:** open. David's call.

## 2026-04-25 ~11:00 — `python-multipart` not in iMac venv

**Discovered while:** dashboard restart crashed at import-time on the multipart dependency.
**Observation:** `/api/models/classify-upload` (added this session for Pi's Model Lab upload-test) uses FastAPI `UploadFile`, which requires `python-multipart`. iMac's `venv` didn't have it; venv-coral on Pi did because we installed it for tests. iMac dashboard wouldn't start until I `pip install python-multipart`.
**Why it matters:** Cleanest long-term fix is a `requirements.txt` (or pinned `pyproject.toml`) so both iMac venv and Pi venv stay in sync. Right now the only protection is "Claude installs a missing dep when noticed" — fragile.
**Status:** open. `pip freeze > requirements.txt` + commit, then `pip install -r requirements.txt` in CI/setup. ~10 min.

## 2026-04-25 ~11:00 — static-vs-Python serving mismatch is a foot-gun

**Discovered while:** debugging the review UI button non-response.
**Observation:** `dashboard/index.html` is a static file served fresh from disk on every request. `dashboard/api.py` is loaded once at uvicorn process startup. So editing both and not restarting → mismatch (new client JS, old server routes). The 404s from the old server cause `reviewSubmit2()` to throw, the catch handler eats it, no advancement.
**Why it matters:** This bit us TODAY. It will bite again. Three possible mitigations:
- (a) Add a build-time version stamp injected into both files; client warns on mismatch
- (b) Change uvicorn launch to use `--reload` in dev (production stays no-reload)
- (c) Standing checklist: "after dashboard/api.py edit → restart dashboard"
**Status:** open. (c) is free and would have caught this. Worth adding to the gotchas doc.

## 2026-04-25 ~11:00 — Pi-Claude shipped multi-model Hailo + watchdog fix

**Discovered while:** comms channel + repo state inspection.
**Observation:** Pi-Claude (parallel session) has:
- Built `pipeline/hailo_engine.py` (the shared-VDevice scheduler pattern from playbook §9 Path 1)
- Modified `pipeline/model_registry.py::build_default_registry` — removed `exclude_hailo=True` parameter; hailo candidates are now `available` based purely on HEF presence
- Fixed a latent watchdog bug in `pipeline/frame_capture.py` + `pipeline/hires_ring.py` (dead-on-startup race: ffmpeg could die before producing first frame, watchdog never fires)
- Updated the Hailo playbook with their findings
**Why it matters:**
- iMac bird-pipeline imports `pipeline/frame_capture.py` and (potentially) `pipeline/hires_ring.py` — Pi-Claude's watchdog fix WILL be picked up on next iMac bird-pipeline restart. That's a strict bug fix, not a behavior change.
- iMac bird-pipeline does NOT import `pipeline/hailo_engine.py` (that's Pi-only via PI_MODE).
- iMac dashboard imports `pipeline/model_registry.py` indirectly (lab registry) — but the change there is parameter removal that only matters under PI_MODE=1. Should be a no-op on iMac.
- Need to verify before next iMac bird-pipeline restart that nothing breaks.
**Status:** open. Worth a sanity check before next iMac restart — confirm `model_registry.py` still works under PI_MODE unset.

---
