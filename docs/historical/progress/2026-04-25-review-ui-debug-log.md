> **HISTORICAL** — This document describes a design, plan, or system state that has been superseded. Kept for decision-trail context.

# Review UI buttons not advancing — debug log

**Symptom (David, 2026-04-25 ~10:50 ET):** "i cant classify anything, the correct, wrong etc buttons dont move to the next bird or seem to be doing anything at all"

**Status: RESOLVED** — buttons working again after iMac dashboard restart + python-multipart install + browser hard-reload.

This doc captures the chronological debug per `superpowers:systematic-debugging`. Reasons noted as we go so we don't forget what bit us.

---

## Phase 1 — Hypothesis + evidence at component boundaries

**Hypothesis:** iMac dashboard never got restarted after this session's `dashboard/api.py` + `dashboard/index.html` edits. Static `index.html` started serving NEW JS immediately. `api.py` is loaded once at uvicorn startup → still OLD code. NEW JS calls `/api/review2/{file}` → OLD server returns 404 → `reviewSubmit2()` throws → catch handler shows toast → no advancement.

Evidence gathered (curls + ps + launchctl):

| Boundary | Result | Reading |
|---|---|---|
| `GET /api/review2/queue` | **404** | OLD api.py — no review2 routes |
| `POST /api/review/x.jpg?verdict=correct` | **200** | OLD api.py — legacy still alive |
| index.html served | 7 `reviewSubmit2` + 4 `/bird-api/review2/` refs | NEW JS being served |
| Dashboard process | uptime **1d 12h 29m** | Started ~April 24 13:30, before the airtight review commits |

**Hypothesis confirmed.** Half-migrated state: NEW client + OLD server.

## Phase 4 — Fix + verification (and the second bug it surfaced)

**First fix attempt:** `launchctl kickstart -k gui/$(id -u)/com.vives.bird-dashboard`.

Process started — but port 8099 didn't respond. New process count went to 0 within seconds. Crash log:

```
File "/Users/vives/bird-classifier/dashboard/api.py", line 2434, in <module>
    @app.post("/api/models/classify-upload")
...
RuntimeError: Form data requires "python-multipart" to be installed.
```

**Second bug surfaced:** `/api/models/classify-upload` (added this session for Pi's Model Lab upload-test) uses FastAPI `UploadFile`, which requires `python-multipart`. iMac's `venv` didn't have it. uvicorn fails at import-time → no server.

**Single fix per Phase 4:** install the missing dep, then restart again.

```bash
/Users/vives/bird-classifier/venv/bin/pip install python-multipart
launchctl kickstart -k "gui/$(id -u)/com.vives.bird-dashboard"
```

Post-restart verification matrix (all green):

| Check | Expected | Actual |
|---|---|---|
| Process running | yes | uvicorn etime 00:08 |
| `GET /api/review2/queue` | 200 | ✓ 200 |
| `POST /api/review/x.jpg` (legacy) | 410 (retired) | ✓ 410 |
| `POST /api/review/batch-confirm` (legacy) | 410 | ✓ 410 |
| End-to-end review2 submit | `history_id`, `duplicate:false` | ✓ |
| Idempotent replay (same client_id) | `duplicate:true` | ✓ |
| `review_history` table exists | created on first DB write | ✓ created automatically |

**Third (latent) thing surfaced:** the iMac `review_history` table didn't exist before today. The new `_ensure_tables_initialized` in `reviews_db.py` creates it on first `get_conn(readonly=False)` call, so as soon as the new code touched the DB the table appeared. Self-healed. The 1,827 historical reviews in the legacy `reviews` table are NOT backfilled — see `2026-04-25-side-findings.md`.

David then hard-reloaded the browser and confirmed: buttons advance and persist.

## Reasons (lessons in plain English)

1. **Editing `dashboard/api.py` without restarting the dashboard is a foot-gun.** The Python-served routes only refresh on uvicorn restart, while the static `index.html` (also in `dashboard/`) is served fresh on every request. So editing both creates an immediate client/server mismatch. The browser starts hitting routes that don't exist, the catch handler eats the 404 silently, and the symptom is "nothing happens."

2. **Adding `UploadFile` (or any FastAPI form-data endpoint) requires `python-multipart` in the venv.** It's a transitive runtime dep that FastAPI tries to import at route-registration time, so the whole module fails to load if it's missing. We added the route on Pi (where venv-coral had it from earlier test work) but not on iMac.

3. **The new airtight review code creates `review_history` on first DB write.** `reviews_db._ensure_tables_initialized` runs on first `get_conn(readonly=False)`. So the table didn't exist on iMac until 11:00 today. The new code self-heals; old rows are not backfilled.

4. **My self-handoff already flagged "iMac dashboard hasn't been restarted to pick up the new airtight review code"** — but framed it as "David's call when, not urgent." That was wrong. Once both files were edited, the iMac dashboard was in a half-broken state for any review action. Should have either restarted immediately on commit, or shouted "DO NOT REVIEW UNTIL RESTARTED" in the commit message.

5. **`systematic-debugging` worked.** The temptation when "just restart it" feels obvious is to skip Phase 1 evidence-gathering. Going through it caught the second bug (multipart) early — if I'd just kicked the service, the first restart would have crashed and I'd be debugging that next, with no map of what's wrong. Phase 1 took 30 seconds of curls + a `ps`. Cheap insurance.

## Side-effect changes that landed today as part of this fix

- `python-multipart 0.0.26` installed in `/Users/vives/bird-classifier/venv/`
- `review_history` table created in `~/bird-snapshots/logs/classifications.db` (1 row from end-to-end test)
- iMac dashboard now runs the airtight review code (legacy POST endpoints return 410)
- iMac dashboard now serves the new `index.html` JS (uses `/api/review2/*` exclusively)
