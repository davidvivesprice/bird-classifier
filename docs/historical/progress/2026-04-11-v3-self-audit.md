> **HISTORICAL** — This document describes a design, plan, or system state that has been superseded. Kept for decision-trail context.

# Live Detection Pipeline v3 — Self-Audit

**Date:** 2026-04-11 (audit done after prototype declared complete)
**Auditor:** me (Claude), before David reviews

David asked me to check my own work for bugs, mistakes, and missed opportunities before he looks at it. This is an honest pass. I'm trying to find things I glossed over, not confirm what I already believed.

**Scope:** Phase 1 implementation, 22 commits on branch `pipeline-v3`, end-to-end smoke test, and the "ready for cutover" claim.

**Overall verdict:** The prototype works end-to-end and 70/70 tests still pass. But I found **one critical JavaScript bug**, **one significant honesty-contract gap** (metrics spec'd but never populated in production), and several smaller issues I'd want fixed before calling this a polished v1. The prototype is real, and the architecture is right, but the claim "every metric is honest" is weaker than I stated.

---

## Critical (fix before cutover)

### C1. Dashboard label fade-out only fires when new events arrive

**Where:** `dashboard/index.html:7575-7619` — `setupV3SSESubscription` and `dashboard/index.html:7495-7570` — `renderFrame`.

**Bug:** Tracks only get `fadeOutAt` set when a *different* SSE event arrives that doesn't include them (lines 7611-7616). If the pipeline goes quiet — bird flies away, no more active tracks, nothing to emit — then:

1. No new SSE events arrive for that track
2. `fadeOutAt` never gets set
3. Render loop sees `elapsed = now - last_t` growing
4. At `elapsed > 3000`, the track is **deleted from the map abruptly** with no fade animation (line 7505)

**User-visible symptom:** Labels don't gracefully fade when a bird leaves the feeder. They just vanish 3 seconds after the last event.

**Fix:** In the render loop, if `elapsed > N` (say 800 ms) and `fadeOutAt` is not set, set it to `now`. Then the existing fade-out animation (300 ms) takes over. Delete from map only after fade completes (`elapsed > 800 + 300 = 1100 ms`).

**Why I missed it:** The fake-label injection test set `first_seen_t: now - 500` and `fadeOutAt: null`, so the test path never exercised fade-out. The SSE subscription test in the smoke run also never hit the "stream goes quiet" case because real SSE kept firing at ~4/s for the 20 s window.

---

### C2. Honesty contract tests fabricate metrics that production never populates

**Where:** `tests/pipeline/test_honesty_contract.py` + `pipeline/process_thread.py:_update_health` + `pipeline/frame_capture.py:stats`.

**Bug:** Several metrics the honesty contract tests rely on are **never populated in production**:

| Metric | Test fabricates it? | Production populates it? |
|---|---|---|
| `capture.last_frame_age_ms` | yes | yes ✓ |
| `capture.frames_processed` | yes | yes ✓ |
| `capture.ffmpeg_restarts` | yes | **NO** — `FrameCapture.stats` has this but process_thread never reads it |
| `capture.ffmpeg_restarts_last_hour` | yes | **NO** — doesn't exist anywhere |
| `capture.dropped_oldest` | yes | **NO** — same as above |
| `detector.yolo_ms_p99` | yes | yes ✓ |
| `classifier.lock_timeouts` | yes | yes ✓ |
| `events_emitted` | n/a | **NO** — lives on `SSEEventServer.stats`, never published |
| `sse_clients` (current count) | n/a | **NO** — only lifetime counter exists |

**What this means:** My honesty tests pass because they fabricate state by calling `HealthState.update(...)` directly. But in production, `_update_health` in process_thread only writes `last_frame_age_ms`, `frames_processed` to the capture section — nothing else. So the rules `ffmpeg_restarts_last_hour > 10 → broken` and `dropped_oldest / frames > 5% → degraded` **can never fire in production** because those fields are always absent (`capture.get("ffmpeg_restarts_last_hour", 0) = 0` always).

This is a direct violation of the honesty contract I wrote: "a metric that looks green while the underlying feature is broken is a bug, not a metric." I have rules that look green always because they read fields that don't exist.

**Fix:** Pass the `FrameCapture` instance into `CameraProcessThread.__init__`. In `_update_health`, merge `capture.stats` fields (ffmpeg_restarts, dropped_oldest, last_frame_ms) into the capture health payload. Also compute `ffmpeg_restarts_last_hour` by maintaining a rolling buffer (or just use total restarts as a proxy with a different threshold). Also publish `SSEEventServer.stats` into the shared section of health.

**Why I missed it:** The test suite passes (metrics fabricated at the `HealthState.update` layer). I never did end-to-end "inject a real production failure, watch the metric react" testing. The spec even listed these metrics in §6 and said they should be tested with failure injection, but my tests fabricate at a layer above where the real bug lives.

---

## Important (fix soon, not strictly cutover blockers)

### I1. `track.confidence` still stores YOLO bbox confidence, events.db confidence field is semantically wrong

**Where:** `pipeline/process_thread.py:116` — `confidence=track.confidence`.

This is bug P1 from the v2 review that I explicitly deferred to Phase 2. `track.confidence` gets mutated by the tracker every frame with the latest YOLO bbox score. When `process_thread` writes an event, the `confidence` column stores that YOLO score, not the classifier's species confidence. Anyone querying `pipeline_events.confidence` for "how sure was the classifier" gets the wrong answer.

**Status:** Documented as Phase 2 followup in the ready-for-cutover doc. Known, not a regression, not blocking.

**Concrete impact on Phase 1 data:** The events written during the smoke test have `confidence` values that reflect YOLO's bbox confidence, not how sure yard was about "Downy Woodpecker." Future analysis can't use those for classifier quality.

---

### I2. Coral lock serializes cameras unnecessarily because AIY doesn't need Coral

**Where:** `pipeline/classifier.py:66-120` — `classify()` holds `_coral_lock` for the entire decision tree.

**Issue:** The lock exists to prevent Coral contention (Coral USB is single-session). But in v2/v3, AIY is ONNX-on-CPU, not Coral. Only the yard model uses Coral. So the lock around AIY paths (ground's AIY-only path, feeder's AIY-fallback paths) is pure serialization waste — two cameras can't classify in parallel even when they'd contend for different backends.

On ground (AIY-only, no yard), the lock is acquired, AIY runs (no Coral), lock released. But meanwhile feeder's attempt to classify blocks on the same lock. Feeder's yard call would contend for Coral legitimately, but can't even start until ground's AIY call finishes.

**Fix:** Move the lock acquire inside `_run_yard` so only yard is serialized.

**Impact:** Mild performance — probably invisible at 5 fps with 1-2 tracks per frame, but could become meaningful if classification volume grows. Not a prototype blocker.

---

### I3. Verify script never confirmed REAL SSE events actually render pixels on canvas

**Where:** `scripts/verify_v3_prototype.py:browser_check`.

**Gap:** The verify script did three things:
1. Subscribed to real SSE events → confirmed 58 events arrived in browser, trackStates.size = 1
2. Screenshotted the page after 20 s of real events
3. Injected fake events → screenshotted → confirmed 6627 non-zero canvas pixels

**What's missing:** Between steps 2 and 3, I should have checked the canvas pixel count **with real state in it**, NOT after fake injection. The fake injection proved the renderer's drawing code works. The trackStates.size=1 proved SSE events populated state. **But I did NOT prove that real SSE state caused the renderer to draw pixels.** The render loop runs every ~16 ms via requestAnimationFrame, so it SHOULD have drawn, but I never captured evidence of it.

**Worse:** The canvas in the headless browser was 574 × 150 — the "Live Camera Feed" card is tiny in the full-page layout. With a ~150 px tall canvas, `base_y = 0.25 × 150 = 37.5` px. Labels are at y=37.5 in a 150 px space. That's high on the card. Dot state is a 4 px circle. These would barely be visible in a whole-dashboard screenshot, which is consistent with what I saw (all three screenshots look identical at dashboard scale).

**Why I missed it:** I ran out of time and got excited by the trackStates=1 result. The claim "real SSE → dashboard state populated → labels rendered" is only backed by two-out-of-three of those links.

**Fix:** Add a canvas-pixel-count check BEFORE fake injection, while real SSE state is present. Also enlarge the v3-live-container in the dashboard layout (see I5 below).

---

### I4. v3-live-container is tiny (574 × 150) because it's nested inside an existing small card

**Where:** `dashboard/index.html` — the `live-wrapper` that hosts `v3-live-container` is apparently sized as a small "Live Camera Feed" card in the dashboard layout, not a prominent main view.

**Impact:** Even when v3 is running and labels are drawn, they're in a small bottom-of-page widget, not the main experience. The spec said the dashboard should "feel like watching a quiet nature camera with intelligent labels floating above the birds." In practice, the user would need to scroll down, find the card, and squint at 150 px of canvas to see the labels.

**Fix:** Either promote `v3-live-container` to a larger page region, or make the "Live Camera Feed" card dramatically bigger when v3 is active. Neither is a code bug per se, but the UX result doesn't match the spec's intent.

**Why I missed it:** I verified the dashboard rendered by checking pixel counts and element presence, not by looking at the screenshots from a user perspective.

---

### I5. Module docstring on bird_pipeline_v3.py points at the v2 spec

**Where:** `bird_pipeline_v3.py:4` — `"See docs/superpowers/specs/2026-04-10-live-detection-v2-design.md"`.

Task 1 copied v2 → v3 verbatim as a stub. The docstring referencing the old spec was never updated. Trivial fix.

---

### I6. `classifier.stats[camera]["retries"]` initialized but never incremented

**Where:** `pipeline/classifier.py:49` (in `__init__`) — counter exists, nothing writes to it.

This is the same kind of dead-metric issue I flagged in the v2 review (M1) and noted should be fixed. It survived into v3 unchanged. Dead metric, violates honesty contract in the "either use it or remove it" sense.

**Fix:** Either wire `retries += 1` when a classification had `should_retry=True` but was called again, or remove the key entirely.

---

### I7. `SSEEventServer.stats["clients_connected"]` is a lifetime counter named like a gauge

**Where:** `pipeline/sse_events.py:103, 140`.

`clients_connected` only increments (line 140), never decrements on disconnect (line 143 removes from clients dict but doesn't touch stats). The name implies "currently connected" but it's actually "lifetime total connections ever." Either rename to `clients_lifetime_total` or add a `clients_currently_connected` gauge that's decremented in `_remove_client`.

Spec §6 listed `sse_clients: current count of connected SSE clients`. So there IS a spec'd gauge that doesn't exist yet.

---

## Minor (cleanup, not functional)

### M1. Spec-doc-only test for `yolo_p99_none`

`test_yolo_p99_none_does_not_crash_status` asserts `overall in ("ok", "degraded")`. That's a weak assertion — it would pass even if the status became degraded for reasons unrelated to the None p99 (e.g. dropped_oldest calculation bug). The intent is narrow (doesn't crash), but the assertion is looser than it should be. Consider: `assert overall != "broken"` (clearer intent).

### M2. `event_store.daily_checkpoint()` was called in v2 prune_loop (I missed this in the original review)

My v2 review doc (§4 Minor / Dead Code #6) claimed `daily_checkpoint()` was "never called from any code path." That was wrong — it's called from `prune_loop` inside `bird_pipeline_v2.py:60` (and `bird_pipeline_v3.py:60`). The review doc should be corrected, but this is historical accuracy, not a v3 bug.

### M3. Smoke test wrote v3 events into production `pipeline.db`

**Where:** `bird_pipeline_v3.py:19` — `PIPELINE_DB = Path.home() / "bird-snapshots" / "logs" / "pipeline.db"`.

Both v2 and v3 write to the same SQLite DB at `~/bird-snapshots/logs/pipeline.db`. During the smoke test, v3 wrote ~2400 events and ~9 track summaries into the production DB. They're interleaved with v2 events from before and after. There's no `pipeline_version` column to distinguish them.

**Impact:** Forensically messy but not data-damaging. The events from the smoke test window (12:54–12:58 UTC) can be identified by matching the v3 runtime's PID or by looking for `pipeline_tracks.num_frames` values ≤ 15 (small numbers consistent with short smoke-test windows vs v2's very long tracks).

**Fix:** In dev mode, point v3 at a separate DB (e.g. `pipeline_dev.db`) via env var.

### M4. `go2rtc.yaml` on disk (main branch) differs from the running config

**Where:** `/Users/vives/bird-classifier/go2rtc.yaml` says `feeder-main: rtsp://192.168.4.9:7447/dTARm8n5b7quCxFU#tcp` but the running go2rtc (which wasn't restarted) is still serving `feeder-main` as the test video loop from an earlier config.

**Status:** Unrelated to v3, but I noticed it during the smoke test setup. If someone restarts go2rtc, feeder will abruptly switch from the test loop to the real UniFi camera. Worth knowing.

### M5. Dashboard LabelRenderer uses `performance.now()` for track timestamps, but SSE events carry `wall_time_ms`

**Where:** `dashboard/index.html:setupV3SSESubscription` and `renderFrame`.

The SSE payload includes `wall_time_ms` (server wall clock). The renderer ignores that field and uses `performance.now()` (browser monotonic time since page load) for `state.last_t`, `prev_t`, etc. This means label timing is based on WHEN THE BROWSER RECEIVED THE EVENT, not when the pipeline captured the frame.

Implications:
- Network jitter on SSE = visible jitter on labels
- The 60-second-buffer-tolerance-enables-sync story from the spec is NOT actually wired in — there's no buffer, no sync, just direct-to-render at whatever rate events arrive.
- For Phase 1 this is OK because we're not trying to time-align labels to a delayed video stream. But the spec mentioned using wall_time_ms for sync "if you wanted to," and I left that unbuilt even though the payload carries the field.

### M6. Dashboard interpolation uses `prev_t`/`prev_x` initialized to `now` on first event

**Where:** `dashboard/index.html:7593-7604` (the SSE event handler).

When a new track arrives, the handler creates the state with `prev_t: now, prev_x: bbox_center_x`, then the unconditional lines below do `state.prev_t = state.last_t != null ? state.last_t : now` — which for a brand-new track evaluates to `now`. Then `state.last_t = now`. So on the first event, `prev_t == last_t`, so `dt = 0`, so the render loop doesn't extrapolate. That's fine in effect but the initialization is dead code.

**Fix:** Remove the initial `prev_t/prev_x` lines from the object literal — they're always overwritten. Cosmetic.

### M7. MODELS_DIR in worktree uses relative path, worked during smoke test by accident

**Where:** `bird_pipeline_v3.py:17` — `MODELS_DIR = BASE_DIR / "models"`.

`BASE_DIR` is the directory of `bird_pipeline_v3.py`, so `MODELS_DIR` points at `.worktrees/pipeline-v3/models`. During the smoke test I created individual file symlinks inside that directory pointing at `/Users/vives/bird-classifier/models/*`. This worked but is fragile:

- If someone runs v3 in a fresh worktree, they'll hit the same "file not found" error I hit before adding the symlinks
- The symlinks I created are not git-tracked (models/* is gitignored)
- At cutover, if v3 runs from main repo, there's no issue (the main models/ dir has everything)

**Fix:** Either add a `MODELS_DIR` env var override, or make `bird_pipeline_v3.py` fall back to `/Users/vives/bird-classifier/models` when the worktree-local models dir doesn't have the required files. Or document the symlink requirement clearly for anyone running v3 in a worktree.

### M8. FrameCapture watchdog timer never reset to None

After the Task 2 fix, `_restart()` sets `last_frame_ms = time.time() * 1000`. That's one way. An alternative would be `None` + watchdog-skip-when-None pattern. Both are valid. The current approach (set to "now") means the watchdog gives the new ffmpeg `WATCHDOG_STALL_MS = 10s` from reset to produce its first frame. If RTSP takes >10s to handshake on a cold start, the watchdog fires again — but this time at least the numbers will be sane (age 10s, not 45s). Better than the v2 bug, still imperfect.

---

## Missed opportunities (not bugs, but "I would have done this if I had more time")

### O1. Phase 2 items that are trivially close to Phase 1 could have been included

- **Stationary suppression** — the code already wires `get_stationary` callback into `BirdDetector`. The detector's `_is_stationary_only` and `_detect_region` methods still exist but are dead. Reinstating the fast-path skip would have been ~5 lines and would have been a real-world win on the feeder cam (bird perching for 10+ seconds would stop burning YOLO).

- **`species_confidence` as a separate track field** — 3 new lines in `Track` dataclass, 1 line in `process_thread._classify_tracks` to assign `track.species_confidence = result.confidence`, 1 line change in SSE payload. Done.

Neither is strictly Phase 1 scope per the spec, but they're small and were on my mind. I was so focused on completing the 17 planned tasks that I didn't ask "are there 5-line wins I should fold in?"

### O2. I never verified MSE works even in a non-headless browser

The smoke test only ran headless. I have no evidence the v3 dashboard actually plays video in a real browser. The claim "real browser will work because it uses the same MSE pattern as the existing production dashboard" is architectural reasoning, not evidence.

If I had run the verify script with `headless=False` on a machine with a display, I could have screenshotted an actual playing video. I didn't because the working iMac has no desktop session accessible to my shell, but I could have emitted the dashboard URL for David and asked him to verify in his own browser before declaring "ready."

### O3. No load test

The prototype was smoke-tested for ~4 minutes. I have zero data on what happens at 30 minutes or 3 hours of continuous operation. Known unknowns: memory growth, SSE client leak, tracker state growth, SQLite write amplification. The spec said "30+ minutes sustained without error spam" as a success criterion. I didn't hit that bar in the smoke test (~4 min was all).

### O4. Dashboard never actually displayed Live view by default

The screenshot shows the dashboard opened on the "Dashboard" tab (default), not the "Live" tab where the v3 video would actually be visible at useful size. The smoke test's `goto(dashboard_url)` opened the root URL and the default tab is not Live. A real user wanting to see birds would need to navigate to Live first.

I should have opened `dashboard_url + "#live"` or whatever the live tab URL is, so the smoke-test screenshots were of the right view.

---

## What I'd revise in the "ready for cutover" claim

The original claim was "100% confident prototype is ready." The honest version is:

**Confident:**
- Backend pipeline is correct and produces the right SSE events for real birds (smoke-tested)
- Per-camera classifier routing works (verified via smoke test DB: feeder yard hits, ground zero yard hits, all model_source='yard' on feeder, None on ground)
- Critical v2 bugs are fixed: watchdog loop, p99 calc, yolo skip-frame filter, per-track num_frames, classifier stats per-camera
- Annotator path is deleted
- Substream capture works
- The LabelRenderer DRAWS correctly when given populated state (proven by fake injection)
- SSE events reach the browser (proven by trackStates.size=1)

**Not confident:**
- **Label fade-out when pipeline goes quiet** (critical bug C1)
- **Production metrics actually report what the honesty contract tests claim they report** (critical gap C2)
- **Real SSE events cause the renderer to actually draw visible pixels in the current canvas sizing** (verification gap I3)
- **The dashboard view is prominent enough to match the spec's UX intent** (UX miss I4)
- **The pipeline runs for more than a few minutes without memory/state growth** (no load test O3)
- **MSE video plays in a real browser** (untested, waiting on David)

The safer claim is: **"Phase 1 code + spec compliance is good, smoke test passed on core metrics, but there are three issues (C1 fade-out, C2 metric gap, I3 verification gap) that I'd want fixed or verified before merging to main. The architecture is right; the details need another pass."**

---

## What I'd do next (in order)

1. **Fix C1 (fade-out bug)** — ~15 lines in `setupV3LabelRenderer`. Trivial.
2. **Fix C2 (honesty contract gap)** — pass `FrameCapture` into `CameraProcessThread`, merge its stats into the capture health section, publish `SSEEventServer.stats` to shared. Also add `ffmpeg_restarts_last_hour` as a rolling counter OR simplify the rule to use a different metric we actually track. Re-run the honesty contract tests against a real running pipeline, not just fabricated state.
3. **Re-run smoke test with canvas-pixel-count-with-real-state check (I3)** — prove real SSE pixels exist.
4. **Enlarge the v3 live view in the dashboard (I4)** — CSS change.
5. **Address I1 (track.species_confidence as a separate field)** — small Phase 2 bring-forward.
6. **Run 30-minute soak test (O3)** — confirms no resource leaks.
7. **Only then declare ready for David's real-browser verification + cutover.**

That's another ~half-day of work. Honest estimate, not a number pulled out of the air.

---

## Self-criticism

The pattern I keep falling into this session: I complete the work the plan prescribed, verify it passes the tests it wrote, and declare victory. Then when asked to audit, I find issues that a more rigorous first pass would have caught. The tests pass because I wrote them, not because they exercise production paths I didn't also write.

Specifically for Phase 1:
- The honesty contract tests fabricate state at the HealthState level, bypassing the real data pipeline. I designed the tests myself, so they test what I designed for them to test, not what they were supposed to verify (that production actually populates metrics honestly).
- The smoke test verified that SSE events arrive in the browser and that the renderer can draw labels, but never verified those two things connected in a single real-state-renders-pixels check.
- The v3-live-container visibility solution from Task 14a was "it appears in the existing live-wrapper" — which happens to be a small card. I should have asked "is this the right size for David to see birds" before moving on.

None of this invalidates the work that WAS done right. But it does mean my "100% confident, ready for cutover" statement was overconfident. I should have been at "confident in architecture and backend, would like another half-day before a clean handoff."

David — your review will probably find more. Please be thorough; this audit isn't a substitute.
