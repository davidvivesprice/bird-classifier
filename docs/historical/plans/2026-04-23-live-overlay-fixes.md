> **HISTORICAL** — This document describes a design, plan, or system state that has been superseded. Kept for decision-trail context.

# /live Overlay Fixes (O-cluster) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans.

**Goal:** Fix three regressions on `/live` David reported on 2026-04-23:
- **O1**: labels drift behind the bird (even though `matched_event_delta` reads ~100ms).
- **O2**: playback sits ~17s behind live edge; target is 10–12s.
- **O3**: labels persist well beyond the intended fade-out (user target: ~1s total).

**Architecture:** O2 and O3 turn out to be two of a kind — both rooted in hls.js config + the `(STALE_MS + FADE_OUT_MS)` constants. O1 was *already fixed* on April 18 (the clock was correctly ported from wall-clock to video-clock via `displayedFrameWallMs()` — verified in recon); the drift David observed may be a symptom of O2 (video delay compresses the kernel-evaluation window against events that are mostly in the future relative to the displayed frame, biasing the anchor). Fixing O2 should measurably reduce O1.

Recon results (2026-04-23):
- `live.html:269` `displayedFrameWallMs()` uses sidecar-derived video clock. Correct.
- `live.html:619` `adaptiveAnchorAt(trackId, series, displayedWallMs)` — clock input is correct.
- `live.html:141-143` `DELAY_SECONDS=8`, `STALE_MS=1200`, `FADE_OUT_MS=400`.
- `hls` config at `live.html:186-194`: `liveSyncDuration: 8`, `liveMaxLatencyDuration: 18`, `maxBufferLength: 23`, `maxLiveSyncPlaybackRate: 1.1`.
- m3u8 playlist healthy: 15 segments, 70s window, live edge fresh.
- ffmpeg uptime 6h — no recent crash/recovery.
- `pipeline/hls_recorder.py:204` has `cleanup_old_chunks(retention_days=7)`. **CORRECTION after second pass**: this IS wired up — `bird_pipeline_v3.py:83 prune_loop` calls it hourly. 620 `.ts` files on disk (oldest 2026-04-16 17:24, ~6.5 days old) are within the 7-day retention, i.e. steady state for the current ffmpeg respawn rate (~6 restarts/day × 15 stranded segments/restart × 7 days). Not a leak. Task 3 below is downgraded from "wire up cleanup" to "add a log line so future regressions are observable."

**Tech Stack:** `dashboard/live.html` (client), `pipeline/hls_recorder.py` (server-side rotation), `bird_pipeline_v3.py` (startup wiring), no new dependencies.

---

## File Structure

**Files modified:**
- `dashboard/live.html` — hls.js config (`liveMaxLatencyDuration`, `maxLiveSyncPlaybackRate`), fade constants (`STALE_MS`, `FADE_OUT_MS`), add anchor-window diagnostic.
- `bird_pipeline_v3.py` — call `HlsRecorder.cleanup_old_chunks()` on startup.
- `pipeline/hls_recorder.py` — minor: ensure cleanup can be scheduled periodically (already has retention_days param).

**No new files.** No backend API changes. No DB changes.

---

### Task 1: O2 — tighten hls.js live-sync config

**Hypothesis:** hls.js sits at 17s because `liveMaxLatencyDuration=18` allows it to. Tightening this and bumping catch-up playback rate should drive it toward the 10–12s target.

- [ ] **Step 1.1: Change constants in `dashboard/live.html`**

  Lines 186–194 currently:
  ```js
  hls = new Hls({
    liveSyncDuration: DELAY_SECONDS,            // 8
    liveMaxLatencyDuration: DELAY_SECONDS + 10, // 18
    maxBufferLength: DELAY_SECONDS + 15,        // 23
    backBufferLength: 10,
    maxLiveSyncPlaybackRate: 1.1,
    enableWorker: true,
    lowLatencyMode: false,
    debug: false,
  });
  ```

  Change to:
  ```js
  hls = new Hls({
    liveSyncDuration: DELAY_SECONDS,              // 8 (unchanged)
    liveMaxLatencyDuration: DELAY_SECONDS + 4,    // 12 (was 18)
    maxBufferLength: DELAY_SECONDS + 8,           // 16 (was 23)
    backBufferLength: 6,                          // was 10 — less memory held
    maxLiveSyncPlaybackRate: 1.5,                 // was 1.1 — faster catch-up
    enableWorker: true,
    lowLatencyMode: false,
    debug: false,
  });
  ```

  Rationale:
  - `liveMaxLatencyDuration: 12` forces a catch-up seek when drift exceeds 12s. Prevents the "just sit at 17s" behavior.
  - `maxLiveSyncPlaybackRate: 1.5` means hls.js plays 1.5× speed during catch-up — closes a 4s gap in ~8 seconds instead of ~40.
  - `maxBufferLength: 16` and `backBufferLength: 6` reduce memory pressure and keep the active window tighter.

- [ ] **Step 1.2: Verify server serves the new config**

  ```bash
  curl -sS http://localhost:8099/live.html | grep -A2 "liveMaxLatencyDuration"
  ```
  Expected: output shows `liveMaxLatencyDuration: DELAY_SECONDS + 4` (not `+ 10`).

- [ ] **Step 1.3: Evidence gate (deferred to David, when available)**

  Hard-refresh `/live`, watch the debug panel (D key) for ~60 seconds. Expected: `Delay (sidecar):` settles around 10–12s, not 17s. If it sits higher than 14s, either the hypothesis is wrong (investigate with `hls.latency` vs sidecar-delta split) or the values need further tuning.

---

### Task 2: O3 — fade constants to 1s total

- [ ] **Step 2.1: Change fade constants**

  Line 143 in `live.html`:
  ```js
  var STALE_MS = 1200;    // time without events before fade-out starts
  var FADE_OUT_MS = 400;
  ```

  Change to:
  ```js
  var STALE_MS = 600;     // time without events before fade-out starts (0.6s)
  var FADE_OUT_MS = 400;  // 0.4s fade — total time from last event to gone = 1.0s
  ```

  Total: 1.0 seconds from last event to invisible. Matches David's preference.

- [ ] **Step 2.2: Verify**

  Same curl grep as 1.2 but against `STALE_MS`.

- [ ] **Step 2.3: O3 root-cause — DOCUMENTED (not fixed)**

  Finding (2026-04-23 recon): `pipeline/tracker.py:85` sets `hit_counter_max = 15`. When a bird leaves frame, Norfair keeps the track alive via Kalman prediction for 15 frames × 200ms = **3 seconds of coasting**, emitting the same (last-known) bbox to SSE the whole time. So the total perceived fade is (3s Norfair coasting) + (STALE_MS=0.6s wait) + (FADE_OUT_MS=0.4s fade) = ~4 seconds wall-clock. That matches David's "has always been long" description.

  Fix options (deferred — each has risk):
  - (a) Lower `hit_counter_max` from 15 to ~5. Minimal change, but could break vote-lock (which needs ≥3 hit-votes — short coasting shortens the window).
  - (b) In `pipeline/process_thread.py`, filter SSE events to omit tracks whose `hit_counter` dropped this frame (i.e. coasting, not real detection). Additive to existing events.
  - (c) Emit a new `is_coasting` flag on each SSE track event; frontend uses it to fade coasting tracks faster.

  **Not changing Norfair behavior in this session** — requires David's review because vote-lock interactions need a soak.

---

### Task 3: HLS cleanup observability (downgraded — cleanup already wired)

Per recon correction above, `prune_loop` already calls `cleanup_old_chunks` hourly with 7-day retention. No leak. Smallest useful change: add a log line so the next regression is observable.

- [ ] **Step 3.1: Add a log line to `prune_loop`**

  In `bird_pipeline_v3.py:83-93`, add `log.info` after the cleanup call:
  ```python
  HlsRecorder.cleanup_old_chunks(hls_root, retention_days=7)
  log.info("[prune] event pruning + hls cleanup done (retention 7d)")
  ```
  One line. Catches regressions where the thread dies silently.

- [ ] **Step 3.2: Verify after restart**

  ```bash
  launchctl unload ~/Library/LaunchAgents/com.vives.bird-pipeline.plist
  launchctl load ~/Library/LaunchAgents/com.vives.bird-pipeline.plist
  # wait for next hourly fire — not immediate
  ```
  Or for an immediate check, invoke manually in a python shell against the same HLS_DIR to confirm no errors.

---

### Task 4: O1 — add instrumentation to catch drift in evidence

O1 requires reproduction to fix; the clock code I read looks correct. Rather than patch blind, add diagnostic output so the NEXT drift incident is provably root-caused.

- [ ] **Step 4.1: Extend the debug panel with anchor math breakdown**

  In `live.html` inside `renderFrame()` (after the `displayedWallMs` line), compute per-visible-track:
  ```js
  // Per track, capture anchor evidence for the debug panel. What we want to
  // know when O1 comes back: is narrow-anchor in the past of T (= label drifts
  // behind bird)? Is velocity calc producing a huge alpha? Is wide vs narrow
  // disagreement large? Capture to window.__o1Debug so the dev-tools user can
  // snapshot it alongside a screenshot.
  if (window.__o1Capture) {
    window.__o1Debug = window.__o1Debug || [];
    var trackSnapshot = {};
    trackSeries.forEach(function(series, trackId) {
      var narrow = gaussianAt(series.events, displayedWallMs, SIGMA_NARROW_MS);
      var wide   = gaussianAt(series.events, displayedWallMs, SIGMA_WIDE_MS);
      var past   = gaussianAt(series.events, displayedWallMs - VEL_LOOKBACK_MS, SIGMA_NARROW_MS);
      trackSnapshot[trackId] = {
        narrow: narrow, wide: wide, past: past,
        events_in_narrow_window: series.events.filter(function(e) {
          return Math.abs(e.wall - displayedWallMs) <= SIGMA_NARROW_MS * 3.2;
        }).length,
        alpha: _adaptiveAlphaEMA.get(trackId),
        last_event_age_ms: displayedWallMs - series.lastSeen,
      };
    });
    window.__o1Debug.push({ t: displayedWallMs, snap: trackSnapshot });
    if (window.__o1Debug.length > 200) window.__o1Debug.shift();
  }
  ```

  Set `window.__o1Capture = true` in the browser console to turn it on. Off by default (overhead).

- [ ] **Step 4.2: Document the capture in the plan doc (no code change)**

  When David next sees O1 drift, ask him to:
  1. Open DevTools Console.
  2. `window.__o1Capture = true`
  3. Wait for a drift incident.
  4. `JSON.stringify(window.__o1Debug.slice(-10))` — paste to me.

  That sequence gives us 10 frames of anchor math. Narrow/wide disagreement, alpha spikes, event-window size — the evidence required to fix the actual cause.

---

### Task 5: Commits

- [ ] **Step 5.1: Commit Task 1 (O2 config)**

  ```bash
  cd ~/bird-classifier
  git add dashboard/live.html
  git commit -m "O2: tighten hls.js live-sync config to close the 17s->10s delay gap"
  ```

- [ ] **Step 5.2: Commit Task 2 (O3 fade constants + investigation note)**

  Commit with a message acknowledging this may be partial (pipeline-side root cause may remain).

- [ ] **Step 5.3: Commit Task 3 (HLS cleanup wire-up)**

- [ ] **Step 5.4: Commit Task 4 (O1 instrumentation only)**

Each commit is independent and evidence-gated where possible.

---

## Self-review notes

- **Spec coverage:** O1 (Task 4 — instrumentation, no speculative patch), O2 (Task 1 — tighter hls.js config), O3 (Task 2 — constants + root-cause note), HLS orphan leak (Task 3 — wire up).
- **Honesty:** O1 fix is *not* in this plan. The plan explicitly says O1 wasn't root-caused. That's the honest state; patching blind is worse than waiting for evidence.
- **Type consistency:** all element IDs, constants, and function names verified against current `live.html`.
- **Risk:** hls.js changes affect every `/live` load. Worst-case: video plays at 1.5× briefly during catch-up (visible as fast-motion). This is intentional and recoverable (reload).
