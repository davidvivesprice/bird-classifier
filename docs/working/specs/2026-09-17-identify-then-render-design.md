> Design from the 2026-09-17 deep pass — DECIDED direction (ROADMAP fork), NOT yet implemented. Lock-latency evidence tables are inline; raw probe outputs were session-scratch. Companion: `2026-05-11-spatial-subtitle-overlay-architecture.md`.

# Identify-then-render — display-path design

Date: 2026-09-17. Author: fleet1 analyst (successor to a9dc4e7c8b278a4ae; its probes are in `EVIDENCE_LOG.md` and were treated as data).
Repo read: `/Users/vives/bird-classifier-pi/` (mirror of the live Pi). Pi probes were read-only (`sqlite3 ... mode=ro`, `journalctl`, `curl /health`); no service was touched, nothing opened `/dev/hailo0`.

Confidence tags: **[M]** measured on this system today, **[S]** source-verified in the repo (file:line), **[E]** estimate, **[A]** assumption to verify in an implementation step.

Builds on: `docs/working/specs/2026-05-11-spatial-subtitle-overlay-architecture.md` ("labels as spatial subtitles over a media clock + deliberate delay"; label states `unknown/candidate/locked/retracted` §BirdCue; delayed-display model `display_delay ≥ classifier_lock_p99 + transport + margin` §Delayed display model) and the shipped video-clock engine (`docs/working/specs/2026-07-02-overlay-video-clock-sync-design.md`; `dashboard/pi_dash.html:1998-2782`). The memo's 8–12 s delay was guessed; this design replaces the guess with the measured lock-latency distribution and adds the one thing neither document has: **retroactive relabel of frames still sitting in the client buffer**, driven by tracker identity events.

---

## 0. What the owner asked for, restated as three properties

1. **Never show an unverified species.** The client shows species text only for tracks whose label is verified (vote-locked, or locked-then-demoted-tentative). Candidate species (votes accumulating) are never rendered.
2. **Retroactive relabel.** When a lock, merge, split or contradiction happens, the label on the frames *still in the client buffer* is rewritten, so the species appears from the first buffered frame of the visit and a wrong revival is scrubbed before it displays.
3. **Modest deliberate delay** chosen from the measured lock-latency distribution so most locks happen before the bird's first frame displays.

Out of scope (coordinator decision): hi-res re-classification (measured today: 1080p crops hurt AIY, 0.959 vs 0.981 on 360p). Server CPU must stay ~0.

---

## 1. Measured lock-latency distribution and unlock timing

### 1a. Production, Pi, 2026-09-10 13:59 → 2026-09-17 14:59 [M]

Method (`lock_latency_prod.txt`, script inline in this dir's evidence): every `classifications.db` row with `extra_json.lock_time` (written once per track on the frame the lock flips, `pipeline/process_thread.py:217-229` → `pipeline/snapshot_writer.py:565,582-603`) joined by `track_id` to the track's first `pipeline_events` row within the preceding 10 min (`pipeline/event_store.py:126-133`; first row = first frame the track was confirmed/active, same moment the first SSE sample is emitted, `process_thread.py:236-256` vs `:259-283`). n = 2923 locks, 0 join failures.

| statistic | wall seconds | ≈ frames @30 fps |
|---|---|---|
| p50 | **0.27** | 8 |
| p90 | **0.94** | 28 |
| p95 | **1.37** | 41 |
| p99 | 6.75 | 200 |
| max | 79.4 | — |

| candidate display delay D | locks that occur before the birth frame would display |
|---|---|
| 0.8 s (today's Smooth `jitterBufferTarget`, `pi_dash.html:2200`) | 86.2 % (2519/2923) |
| 1.0 s | 90.8 % |
| **1.5 s** | **95.4 %** |
| 2.0 s | 96.6 % |
| 3.0 s | 96.9 % |
| 5.0 s | 97.8 % |

Other facts from the same window [M]: 31.2 % of confirmed tracks ever lock (2923/9355); locked tracks last p50 1.23 s / p90 7.6 s, never-labelled tracks p50 0.36 s / p90 2.2 s (mostly flickers); 99.3 % of locks fire on exactly the 3rd vote (`vote_history_len` = 3 in 2904/2923) — consistent with the coordinator's finding that waiting longer changes <1 % of outcomes. Today's hourly p50 ranged 0.14–0.35 s, p90 0.23–2.13 s.

Era caveat: the tracker was v3 (Norfair) for all but the last ~35 min of the window (v4 went live ≈14:25 Pi clock). The lock rule is identical across trackers (`process_thread.py:515-519`) and both confirm a track after 2 hits (`tracker_v4.py:56`, v3 `initialization_delay=2`), so birth semantics are comparable. TTA on the decisive vote was deployed today and is not in most of the window; it makes the 3rd vote slightly slower (~30 ms, `process_thread.py:368-372`) — immaterial at this scale.

Floor: a lock needs 3 votes at `CLASSIFY_EVERY = 2` hit-frames (`process_thread.py:59`), so the fastest possible lock is ≈4 hit-frames ≈ 0.13 s; observed minima 0.13–0.23 s on the reel confirm it.

### 1b. The may10 reel through the real pipeline, tracker v4.4 [M]

Source: `/tmp/grade_last.jsonl` on the Pi (the 14:40 `lab grade` that produced `ab/grade2/grade_v4.txt`: 9 ids for 9 visits, census 6 ✓ / 0 wrong-name / 3 uncovered / 0 phantoms), copied here as `grade_last_pi.jsonl`; analysis `lock_latency_may10_v44.txt` via `lock_latency_may10.py`.

| track | birth pts | lock latency | species | visit |
|---|---|---|---|---|
| 1 | 0.57 | +5.50 s | House Finch | 128.7 s |
| 2 | 25.70 | +0.23 s | Am. Goldfinch | 17.9 s |
| 3 | 30.13 | +2.10 s | Am. Goldfinch | 25.8 s |
| 4 | 30.60 | +0.23 s | Am. Goldfinch | 1.6 s |
| 5 | 44.33 | +7.57 s | Am. Goldfinch | 82.1 s |
| 6 | 85.10 | never | (candidate W-b Nuthatch) | 1.7 s |
| 7 | 87.53 | never | — | 1.5 s |
| 8 | 132.43 | +0.73 s | Tufted Titmouse | 5.6 s |
| 9 | 138.07 | +0.13 s | Blue Jay (split newcomer, `frame_count` starts at 31) | 11.8 s |

n = 7 locks: p50 0.73 s; D = 1.0/1.5/2.0/3.0 s covers 4/4/4/5 of 7. Footage of *locked* tracks that would display unlabelled: 4.7 % at 0.8 s → 3.9 % at 1.5 s → 2.6 % at 3.0 s. The two slow locks (5.5 s, 7.6 s) are the finch/goldfinch confusion pair (grade visits 2, 3, 6 are the misses) — a classifier problem, not a display-delay problem; no delay fixes them.

**The split case this design exists for** (`split_timeline_v44.txt`): locked track 8 ("Tufted Titmouse") went lost at pts 135.5, was colour/space-revived onto the Blue Jay at **137.07**, and rode it **non-coasting, locked, labelled Titmouse for 0.96 s** (23 samples, 137.07 → 138.03) until the vote-split created track 9 at **138.07**; track 9 locked Blue Jay at 138.20. Today's client (`pi_dash.html:2373-2376`) draws "Tufted Titmouse" on the jay for that second. With D ≥ ~1.1 s plus the split hint below, all of it is rewritten before any of those frames reach the glass.

### 1c. Unlocks after lock (bounds the retroactive-scrub window) [M]

`journalctl` 30 days, `UNLOCKED:` lines (`process_thread.py:387-392, 423-429`) joined to the same track's first-lock row: **35 unlocks of 3140 first-locks (1.1 %)** — 24 contradiction unlocks, 11 unverifiable (tentative) unlocks. Seconds after first lock, sorted: 1.2, 2.7, 2.9, 2.9, 3.5, 3.7, 3.8, 3.9, 4.1, 5.5, 6.9, 9.4, 9.6, 14.5, 18.6, 22.6, 25.2, 27.6, 28.9, 31.5, 36.3, 38.1, 38.5, 40.7, 53.6, 62.1, 65.1, 71.1, 76.5, 95.1, 112.5, 132.8, 170.1, 174.8, 211.0.

| within | unlocks |
|---|---|
| 1 s | 0/35 |
| 2 s | 1/35 |
| 3 s | 4/35 |
| 5 s | 9/35 |
| 10 s | 13/35 |

This is structural, not bad luck: a contradiction unlock needs `LOCK_UNLOCK_DISAGREEMENTS = 4` verify votes at `LOCK_VERIFY_EVERY = 15` hit-frames (`process_thread.py:61-62`) → ≥ 60 hit-frames ≈ 2 s after the lock at the very earliest. **Honest conclusion: a modest delay cannot retroactively scrub contradiction unlocks; those will keep flipping on glass (as today), at a 0.8 %/lock rate.** What the delay + hints *do* scrub is the high-harm, fast class: wrong revivals (split within ~1 s, above), young-track merges (fire within `_VOTE_YOUNG_FRAMES` = 90 frames of spawn, `tracker_v4.py:107`), and the "identifying…" prelude of every normal visit. Live identity-event rate today [M]: health at uptime 1148 s showed `reid_revivals 2, vote_merges 0, vote_splits 0` — rare, but each one is exactly the wrong-species-on-the-wrong-bird case.

### 1d. Derived delay

Effective lock budget on LAN ≈ D − (label arrival latency − video display latency) ≈ D − 0.2 s (spec §1a/1b: labels reach the browser at T + 150–400 ms, frames display at T + 100–350 ms + jitter target) [E].

**Recommend D = 1.5 s** (Smooth mode default). It covers p95 (1.37 s) exactly, ≈94 % effective on LAN (interpolated between the measured 1.0 s and 1.5 s points, not separately measured), and it clears the measured 0.96 s split ride with 0.3 s of transport margin. Going to 2.0 s buys +1.2 points for +0.5 s of glass latency; 3.0 s buys +0.3 more for another second — past the knee. Keep it a single constant (`DELAY_MS`, URL/localStorage override) so 2.0 s is a one-value change if the owner prefers the margin. Realtime toggle stays as the D = 0 escape hatch (labels then trail, species text still only when verified).

---

## 2. Architecture (one picture)

```
tracker_v4 ──identity_log (merge/split, seg_pts)──┐
process_thread ── per-track label_state/seg_pts/lock_pts/label_epoch ──┤
                                                                       ▼
                       SSEEventServer.emit(..., identity=[ops])  →  :8105 SSE  →  api.py SSE proxy / WS mirror
                                                                                          │
   go2rtc video (WebRTC jitterBufferTarget = D | MSE playbackRate law → gap = D) ─────────┤
                                                                                          ▼
                    pi_dash.html engine: per-track pts-keyed sample buffers (already exist)
                      + applyIdentityOps (rekey)  + restamp/strip on label transitions
                      → render at T = displayed camera time (unchanged) with the verified-only rule
```
Nothing new is decoded, classified or stored. The Pi adds a few bytes per event; the client rewrites in-memory samples it already holds.

---

## 3. SSE schema additions (backward compatible)

Current payload: `pipeline/sse_events.py:163-174` (top-level `camera, wall_time_ms, pts, seq, emit_ms, tracks`); per-track dict built at `pipeline/process_thread.py:263-277` (`track_id, bbox, bbox_center_x, frame_width, frame_height, species, species_confidence, model_source, is_locked, frame_count, coasting`). Live sample confirms unlocked tracks already carry a *candidate* `species` (`EVIDENCE_LOG.md:3130`: `"species":"Tufted Titmouse","is_locked":false`) — the client must never render that.

### 3a. Per-track fields (state, sent on every event — idempotent, so a reconnecting client needs no history)

| field | type | meaning | set where |
|---|---|---|---|
| `label_state` | `"none" \| "candidate" \| "locked" \| "tentative"` | the only thing the render rule reads | derived in process_thread at payload time from `is_locked` / new `Track.tentative` / `species` |
| `label_epoch` | int | +1 on every transition of the *displayed* state (lock, unlock, relabel while locked/tentative). Candidate churn does **not** bump it. Client uses inequality as the "rewrite buffered samples" trigger | `process_thread.py:518` (lock), `:393` (unverifiable → tentative), `:430-438` (contradiction → candidate/new species) |
| `seg_pts` | float | pts where the track's current contiguous visible segment began (spawn, revival, merge-adopted young). Split rekey boundary + reconnect-safe birth anchor | tracker (§3c) |
| `lock_pts` | float\|null | pts of the frame that established the current lock; kept through tentative; null otherwise | `process_thread.py:518` / `:393` keep / `:430` null |

### 3b. Top-level fields

```json
{ "camera":"feeder", "wall_time_ms":…, "pts":657.1666, "seq":1499, "emit_ms":…,
  "schema": 2,
  "identity": [
    {"id": 17, "op": "split", "from": 8, "to": 9, "at_pts": 137.0667},
    {"id": 18, "op": "merge", "from": 12, "into": 4, "at_pts": 300.1000}
  ],
  "tracks": [ { …existing…, "label_state":"locked", "label_epoch":1, "seg_pts":132.4333, "lock_pts":133.1667 } ] }
```
- `identity` is present only when non-empty. `split.at_pts` = start of the revived segment (`seg_pts` of the revived track at revival time) — samples of `from` with `pts ≥ at_pts` belong to `to`. `merge.at_pts` = `seg_pts` of the young track (informational; all of the young's samples move).
- Each op is emitted on the event of the frame in which `_apply_pending_merges/_apply_pending_splits` ran (they run at the top of `update()`, `tracker_v4.py:282-283`, so the same frame's SSE event carries them) **and repeated on the next 9 events** (~0.3 s). The per-client SSE queue drops events when full (`sse_events.py:21` `CLIENT_QUEUE_MAX = 32`, `:183-187`), so a one-shot op could vanish silently; repetition + client dedupe by `id` covers short stalls. Longer stalls end in a reconnect, where there is no buffer to rekey anyway.
- `schema: 2` lets the client (and tools) branch; absence = today's payload.

Old clients ignore unknown keys (`pi_dash.html:2505-2514` copies named fields only). `tools/score_census.py:24-27` reads only `is_locked`/`species`/`track_id`/`pts` → census output is unchanged by construction (verified in T4 below). The WS mirror forwards the JSON verbatim (`dashboard/api.py:5464-5483, 5486-5507`), so the tunnel path gets the fields for free. `PIPELINE_RECORD_EVENTS` (`sse_events.py:176-180`) records them for free.

### 3c. Server changes, by file

**`pipeline/tracker_common.py:45-74` (Track dataclass)** — add `tentative: bool = False`, `label_epoch: int = 0`, `lock_pts: Optional[float] = None`, `seg_pts: Optional[float] = None`.

**`pipeline/tracker_v4.py`**
- `update(self, detections, frame_time_ms, frame_bgr=None, pts=None)` (`:280`): store `self._pts = pts`.
- `_spawn` (`:524-540`): `trk.seg_pts = self._pts`.
- `_try_reid` (`:509-521`): on revival `trk.seg_pts = self._pts`; `self.revivals += 1` already at `:519`.
- `_miss` frozen snapshot (`:470-476`): add `"tentative", "lock_pts", "label_epoch", "seg_pts"` so a split restores the full label state (restore at `:656-659`).
- `_apply_pending_merges` (`:586-620`): `lost.seg_pts = young.seg_pts`; append `{"op":"merge","from":young_id,"into":lost_id,"at_pts":young.seg_pts}` to a new `self.identity_log: list[dict]` (keep `self.merges` tuples untouched — `tests/test_tracker_v4.py:192,213` assert their shape).
- `_apply_pending_splits` (`:622-671`): `at = trk.seg_pts` (pre-restore = revival pts); `newcomer.seg_pts = at`; restore `trk.seg_pts/tentative/lock_pts/label_epoch` from `fr`; append `{"op":"split","from":tid,"to":nid,"at_pts":at}` (keep `self.splits` tuples — `tests/test_tracker_v4.py:237`).
- Each appended op gets a monotonic `"id"` from `self._identity_seq`.

**`pipeline/tracker.py:128` (v3)** — add the ignored `pts=None` kwarg so `PIPELINE_TRACKER=v3` A/B still runs (`tracker_common.py:93-107`).

**`pipeline/process_thread.py`**
- `:196` → `self.tracker.update(detections, frame.wall_time_ms, frame_bgr=frame.bgr, pts=frame.pts)`.
- Lock `:518-519`: also `track.tentative = False; track.lock_pts = frame.pts; track.label_epoch += 1`.
- Unverifiable unlock `:393-407`: also `track.tentative = True; track.label_epoch += 1` (keep `lock_pts`, keep species — this is the code's own stated intent at `:396-404`).
- Contradiction unlock `:430-441`: also `track.tentative = False; track.lock_pts = None; track.label_epoch += 1`.
- New `_label_state(track)`: `locked` if `is_locked`; else `tentative` if `track.tentative and track.species`; else `candidate` if `track.species`; else `none`.
- Payload `:263-277`: add the four fields.
- New `_drain_identity_ops()`: `log = getattr(self.tracker, "identity_log", None)`; take entries from `self._identity_cursor`; keep a `deque` of `(op, emitted_count)`; return ops with `emitted_count < 10`. Cost: a list comprehension over ≤ a few entries.
- `:278-283` → `self.sse_server.emit(..., identity=ops or None)`.

**`pipeline/sse_events.py:152-175`** — `emit(..., pts=0.0, identity=None)`; add `"schema": 2` and `"identity": identity` only when not None.

**Fakes that must accept the kwarg:** `tools/demo_grade.py:55-58` `_FakeSSE.emit(camera, wall_time_ms, pts, tracks)` and the equivalent in `tools/offline_replay.py` (constructed at `:83`) → add `identity=None` (and record it so the lab can assert ops). `tests/pipeline/test_process_thread.py` fakes likewise.

---

## 4. Client changes (`dashboard/pi_dash.html`, engine `setupLiveView` `:1846-2782`)

### 4a. Buffer depth control

- **WebRTC (Mac/Chrome, iPad LAN if supported):** `applyLatencyPref` `:2194-2203` sets `rx.jitterBufferTarget = smoothPref ? 800 : 0` on the video receiver only. Change to `DELAY_MS` (default 1500; `?delay=` URL param / `localStorage.syncDelayMs`) and apply to **every** receiver (`pc.getReceivers()`), so audio is delayed by the same amount (otherwise bird calls lead the picture by 1.5 s when Sound is on, `:1847-1861`). Range check: `jitterBufferTarget` accepts 0–4000 ms (MDN, `EVIDENCE_LOG.md:3058-3073`) [S]; it is a *minimum playout delay hint* — the engine already measures the achieved value (`sampleVideoBuffering` `:2204-2223`, `est.bVideoMs`) and feeds it into C (`:2255`), so calibration tracks whatever Chrome actually does [S]. Whether Chrome honours 1500 ms fully is **[A]** — measured in step S7 (go/no-go).
- **MSE (tunnel, iPad):** the buffer depth is set by go2rtc's vendored player law `this.video.playbackRate = gap > 0.1 ? gap : 0.1` (`dashboard/video-rtc.js:478-479`): d(gap)/dt = 1 − gap ⇒ equilibrium **1.0 s**, which is why the spec calls iPad's buffer "free Smooth". Parameterize with two lines:
  ```js
  const target = this.mseTargetGap > 0 ? this.mseTargetGap : 1.0;   // seconds held behind the live edge (upstream default 1.0)
  this.video.playbackRate = gap > 0.1 ? Math.max(0.1, gap / target) : 0.1;   // equilibrium gap = target
  ```
  and set `video.mseTargetGap = smoothPref ? DELAY_MS/1000 : 1.0` from `applyLatencyPref`. Constraint: the player trims the SourceBuffer to 5 s behind the live edge (`video-rtc.js:468-473`), so **D must stay ≤ 3.5 s** (assert in code). This keeps the "use go2rtc's video-rtc.js" rule (feedback_video_rtc.md): same file, a 2-line parameterization, upstream-diff minimal, served by `api.py:459-462`. The MSE branch of the buffering probe (`:2224-2229`) already accepts gaps < 10 s; the rig-found −67 ms MSE trim (`:2253-2254`) is unaffected.
- **iPad Safari over WebRTC:** Safari exposes no jitter-buffer knob (spec §Buffer policy: "Safari WebRTC: no knob ⇒ REALTIME always"). In Smooth mode on such a browser, force MSE before assigning the source in `reconnectLiveVideo` (`:1929-1950`, before `video.src = url` at `:1949`): `video.mode = (smoothPref && !canDelayRtc) ? 'mse' : 'webrtc,mse'` where `canDelayRtc = 'jitterBufferTarget' in RTCRtpReceiver.prototype || 'playoutDelayHint' in RTCRtpReceiver.prototype`. Runtime `mode` switching is the exact mechanism the sync rig already uses for its forced-MSE run (`tools/sync_rig.py` "forced MSE run": `h.mode='mse'; h.ondisconnect(); h.src=…`; `video-rtc.js:39,368,379`). Toggling Smooth/Realtime then re-runs `reconnectLiveVideo` when the mode must change (reset `currentSrc` first, `:1931`).
- Button copy `:982-983` (title "~0.8s") → "~1.5 s so every label is verified before its frame shows". Diag chip (`:2665-2694`) gains `delay: target/achieved`.

### 4b. Sample buffer rewrites on hints

Entries: `tracks: Map<tid, {samples[], cursor, …}>` (`:2007, 2273-2299`); insertion `insertTrackSamples` `:2491-2525`; sample shape `:2505-2514` → add `label_state, label_epoch, seg_pts, lock_pts`. Per entry add `labelEpoch`, `verified` (last verified species or null), and counters.

Order per event: **identity ops first, then samples.** Dedupe ops by `op.id` in a bounded `Set` (256, FIFO), cleared in `resetOverlayState` (`:2055-2090`) and in `reanchor(reason, clearBuffers=true)` (`:2103-2114` — pts epoch changed, old ids/ops meaningless).

Rules (all operate on in-memory samples only; the rAF loop `renderFrame` `:2402-2486` reads them at displayed time T, so frames not yet on glass pick up the rewrite; frames already shown cannot be changed — that is what the delay is for):

- **R1 lock / relabel (backfill):** on inserting sample `s` for id X with `s.label_state ∈ {locked, tentative}` and (`s.label_epoch !== entry.labelEpoch` or `s.species !== entry.verified`): for every buffered sample q of X set `q.species = s.species, q.conf = s.conf, q.label_state = s.label_state, q.locked = (s.label_state === 'locked')`. All buffered samples, including those before a revival gap — that is the tracker's belief (same id = same bird); a wrong revival is undone by R4. `entry.labelEpoch = s.label_epoch; entry.verified = s.species`. Counter `stamps++`.
- **R2 retraction (contradiction):** on inserting `s` with `s.label_state ∈ {none, candidate}` for X where `entry.verified !== null`: strip every buffered sample (`species = null, label_state = 'none', locked = false`); `entry.verified = null`. Counter `scrubs++`. (Tentative demotions do **not** strip — the pipeline keeps that species on purpose, `process_thread.py:396-404`; today the client flips it to "identifying…", which this fixes: on the reel, track 1 at pts 33.17 for 0.66 s.)
- **R3 merge `from → into`:** `ensureTrackEls(into)`; move all samples of `from` into `into`'s list (merge by pts; they don't overlap — a merge target was in `_lost`), reset `into.cursor = 0`, empty `from` (the render loop removes an empty entry, `:2445-2446`). If `into.verified` exists, apply R1 to the moved samples with the survivor's state. Counter `rekeys++`.
- **R4 split `from → to at at_pts`:** `ensureTrackEls(to)`; move samples of `from` with `pts ≥ at_pts` into `to`, **strip their label** (they carried `from`'s species), reset both cursors. The next locked sample of `to` (Blue Jay at 138.20 on the reel) triggers R1 and stamps the moved frames from 137.07 onward. Counter `rekeys++`.
- Cursor hygiene: `sampleTrackAt` (`:2306-2328`) assumes chronological order and a monotonic cursor; after any splice set `cursor = 0` (same as the 400-cap branch, `:2522`; one O(n ≤ 400) catch-up, negligible).
- Legacy fallback: if `s.label_state` is undefined (old server), derive `locked ? 'locked' : (species ? 'candidate' : 'none')` so a new client against an old pipeline behaves exactly as today.

Worked example (reel, D = 1.5 s, LAN transport ≈ 0.1–0.3 s): split op arrives at wall ≈ pts 138.07 + 0.3; glass shows T ≈ 138.4 − 1.5 = 136.9 < 137.07 → the entire 0.96 s Titmouse ride on the jay is rekeyed and relabelled Blue Jay before display. With today's 0.8 s, T ≈ 137.6 → 0.5 s of wrong label would already be on glass.

### 4c. Render rule for unverified tracks (`renderTrackDom` `:2340-2388`, text at `:2370-2380`)

Recommendation: **draw the box, no text at all** for `label_state ∈ {none, candidate}`; species text (`Name · 88%`) for `locked` and `tentative`; keep "identifying…" only under `?syncdiag=1` (`:2666`). CSS: `.live-bbox.unverified { border-color: rgba(255,255,255,.55); box-shadow: 0 0 0 1px rgba(0,0,0,.35); }` next to `:309-332`; `.live-label.novisible { visibility: hidden; }` next to `:350-356` (not `opacity`, which the birth/death fades own, `:2381-2394, 2452-2465`).

Why box-without-text rather than a neutral "bird" chip or today's "identifying…":
1. Any text is a claim. "Bird" restates what the box already says and adds clutter on multi-bird frames; "identifying…" is engineering surfacing ("engineering stays invisible", CLAUDE.md), and a chip that flips into a name is exactly the "pending → verdict" theatre the owner's directive rejects. The 2026-05-11 memo already concluded: "live default shows only locked/human_confirmed cues" (§Label states).
2. With D = 1.5 s, ≥ 94 % of locks precede the first displayed frame, so an unverified box is the rare, short state — it should be quiet, not announced.
3. 69 % of confirmed tracks never lock [M] (mostly sub-second flickers, but also whole unlabelled visits — the census "uncovered" cases). A chip that says "identifying…" for an entire 5-second visit that never resolves is a broken promise; a plain box honestly says "we see something, we don't claim what."
4. It is one step from the memo's target ("labels only, boxes optional"): when labels-only ships, unverified simply renders nothing.
`tentative` renders identically to `locked` (no visible difference by default; class `tentative` present for the rig/diag). Realtime mode uses the same rule.

### 4d. Diagnostics / test hooks

`window.__overlayDebug` (`:2698-2767`) gains `delayMs` (target) and per-track `labelState`, `labelEpoch`, and counters `stamps/scrubs/rekeys/opsSeen`; `renderedBoxes` (`:2719-2737`) gains `labelState`. The rig samples `renderedBoxes` every 100 ms (`tools/sync_rig.py` SAMPLER_SETUP) and can therefore count *retractions on glass* (label text for an id changing after it was shown) — the acceptance metric for property 2.

### 4e. Buffer bounds

Samples are pruned behind T at 10 s (`:2442`) and capped at 400/track (`:2522`, resets cursor). At 30 fps a long visit holds ≈ (10 + D + lookahead) × 30 ≈ 350–370 samples — inside the cap but close. Since nothing behind T is ever re-displayed, tighten the behind-T retention to 2 s (`while (s.length > 2 && s[0].pts < T - 2)`) so the cap never engages in normal operation. Memory: 8 tracks × 400 × ~150 B ≈ 0.5 MB worst case [E].

---

## 5. Failure modes

| situation | what happens | handling |
|---|---|---|
| **Reconnect mid-visit** (EventSource auto-reconnect `:2576-2580`; WS retry 1.5 s `:2602-2607`) | The SSE server has no replay (`sse_events.py:190-194`); the client has no samples for the D seconds of video still in flight → bird visible without a box for ≤ D s, then boxes resume. Missed identity ops are irrelevant (nothing buffered to rekey). | Per-track state is idempotent (`label_state/seg_pts/lock_pts` on every event), so the first post-reconnect sample carries the verified label; R1 stamps it onto whatever is buffered. Optional S10: a per-camera ring of the last ~D+1 s of payloads replayed on `_add_client` (`sse_events.py:190`) closes the gap entirely. |
| **Clock recalibration** (`reanchor` `:2103-2114` on transport flip / RTP discontinuity / mediaTime jump) | Buffers keyed by pts survive (`clearBuffers=false`); labels intact. While UNCALIBRATED the renderer shows the newest sample immediately (`:2425-2436`) — the delay is bypassed for ≈5 s (≥20 offset samples at ≤4 Hz, `:2236, 2256`). | The render rule still never shows candidate species; a later-retracted lock could show during that window (rate ≈1 %/lock × 5 s — negligible). Accept; document in the diag chip ("calibrating"). |
| **pts epoch reset** (pipeline restart; ids restart at 1) | `insertTrackSamples` detects `pts < ptsNewest − 1` → `reanchor('pts jump backward', true)` clears all buffers (`:2494-2497`). | Also clear the op-dedupe set and `labelEpoch/verified` per entry there. Video keeps playing (go2rtc unaffected); boxes vanish until the pipeline is back (rig chaos step: re-lock 9.1 s, book 10-overlay-sync.md:83). Old-run ids cannot collide with new-run ops because buffers were emptied. |
| **Tunnel / MSE path** | WS mirror forwards payload verbatim (`api.py:5464-5507`); Cloudflare bursts land in the buffer (`:2488-2490`); MSE depth is deterministic (playbackRate law → gap = D). | No special handling. The −67 ms MSE trim remains. |
| **iPad Safari** | WebRTC has no delay knob; MSE fractional `playbackRate` on iPadOS is what the existing 1.0 s law already relies on, but the book still lists "iPad-over-tunnel wants one manual confirmation of the MSE trim" (10-overlay-sync.md:99-100) as open. | Force MSE in Smooth mode (§4a). Device check with screenshots is a hard gate (feedback_human_facing_verification.md) — step S8. |
| **Hint arrives after its frames displayed** (transport stall > D − ride, or a contradiction — §1c) | Label flips on glass, exactly as today. | Bounded and measured: contradictions 0/35 within 1 s; the reel's split ride was 0.96 s vs D 1.5 s (0.3–0.5 s margin after transport). 2.0 s if more margin is wanted. |
| **SSE queue drops** (`CLIENT_QUEUE_MAX = 32`, drops silent, `seqGaps` counts them `:2533-2536`) | Lost samples = missing box frames (today's behaviour); a lost one-shot op would be a missed rekey. | Ops repeated for 10 events; state fields idempotent. |
| **Buffer overrun on long visits** (reel max 128.7 s; prod p99 27.5 s) | Cap at 400 samples trims oldest and resets cursor. | Retention behind T tightened to 2 s (§4e) so the cap is a safety net, not a working mode. |
| **Wrong merge** (vote-ReID false positive; gated at cosine ≥ 0.60, `tracker_v4.py:106`) | R3 stamps the survivor's species onto the young segment before display. | Same exposure as the tracker itself; the mirror split rule (`:114`) and a later contradiction (R2) undo it. Track `rekeys` vs health `vote_merges` in the soak (T9). |
| **Audio/video skew** with Sound on | If only the video receiver is delayed, audio leads by D. | Apply the target to all receivers (§4a). |
| **Chrome does not honour 1500 ms** [A] | Achieved delay < D → coverage falls toward the 0.8 s numbers; sync itself stays exact because C uses the measured buffer. | S7 measures `jitterBufferDelay/emittedCount`; fallback: force MSE on Chrome in Smooth mode (deterministic law). |

---

## 6. CPU / latency cost on the Pi

- Tracker: four attribute writes at spawn/revival/merge/split; one small dict append per merge/split (events per hour, not per frame). Zero per-frame work.
- process_thread: `_label_state` (three attribute reads) + four dict keys per active track per frame; `_drain_identity_ops` is a slice of an almost-always-empty list. Order of 5–10 µs per frame in CPython [E] against a 33 ms frame budget → < 0.05 %.
- Payload: +~60 B per track per event (~2 KB/s at 30 ev/s with one bird) on top of ~350 B/track today. `json.dumps` cost grows proportionally (~+15 %) on a call that is already sub-millisecond.
- No change to decode, Hailo, classifier cadence, DB writes or snapshot path. Health `yolo_ms_avg`, load average and temperature are the regression check (S4).
- Latency: none added server-side; the deliberate delay is entirely client-side buffering of the video.

---

## 7. Test / acceptance plan

| id | what | where | pass |
|---|---|---|---|
| T1 | tracker: merge/split append `identity_log` ops with correct `from/into/to/at_pts`; `seg_pts` set at spawn/revival/merge; frozen snapshot restores `tentative/lock_pts/label_epoch/seg_pts`; `merges/splits` tuples unchanged | `tests/test_tracker_v4.py` (+ v3 accepts `pts=`) | 12 existing + new pass |
| T2 | process_thread: `label_state` for the four states; `label_epoch` bumps only on lock / unverifiable / contradiction, not on candidate churn; ops emitted on the event of the frame and repeated ≤ 10 events then drained; payload keys present; fakes accept `identity=` | `tests/pipeline/test_process_thread.py` | pass |
| T3 | `emit()` adds `schema:2` and `identity` only when given; payload otherwise byte-identical | `tests/pipeline/test_sse_events.py` | pass |
| T4 | census invariance: inject the new fields into `grade_last_pi.jsonl` and run `tools/score_census.py` → output identical to baseline (`windows 9 \| correct 6 \| wrong-name 0 \| uncovered 3 \| phantoms 0`); later re-run `lab grade` on the Pi (when the lab is free) and diff the CENSUS block against `ab/grade2/grade_v4.txt` | Mac (script) then Pi lab | identical |
| T5 | client engine unit tests in Node 24 (`node --test`, no deps; `/Users/vives/.nvm/versions/node/v24.15.0/bin/node` exists, repo has no `package.json`): extract the pure buffer core (`ensureEntry/insert/applyIdentityOps/rekey/restamp/strip/sampleTrackAt/prune`) into `dashboard/overlay-engine.js` (no DOM), served like `video-stream.js` (`api.py:465-468`) and imported by `pi_dash.html`. Cases: lock backfill (track 1 samples 0.57–6.07 gain House Finch after the lock sample), split 8→9 at 137.07 (no Titmouse on any sample ≥ 137.07; Blue Jay on all track-9 samples ≥ 137.07 after the 138.20 lock), merge, contradiction strip, tentative keeps species, op dedupe, reanchor clears, cursor validity after splice, 400-cap, legacy payload fallback | `tests/js/overlay-engine.test.mjs` (new), run from `pytest` via a subprocess so `tests/` stays one suite | all pass |
| T6 | scripted replay: feed `grade_last_pi.jsonl` (plus real ops once S4 is deployed and `lab grade` re-run) through the engine with a simulated display clock T = arrival_pts − D; count "retraction on glass" (a rendered id whose label text changes after first display) at D = 0.8 vs 1.5 s | Node or Python harness in `tools/` | 0 retractions at 1.5 s for the split case; the 0.8 s run shows ≥1 (proves the metric) |
| T7 | sync rig: add runs `webrtc-delay1500` and `mse-delay1500` (`?delay=1500`), keep gates `\|O_total\| p50 ≤ 40 ms, jitter p90 ≤ 40 ms, spatial p50 ≤ 8 px` (`tools/sync_rig.py:200-203`), add: achieved delay (`est.bVideoMs`) within ±20 % of target on Chrome / ±10 % on MSE, and retractions-on-glass = 0 for the reel apart from contradictions later than D | Mac, ~8 min, demo mode toggles itself | PASS on both transports |
| T8 | device: iPad Safari via tunnel (forced MSE) — label present at the first displayed frame of a visit; MSE trim sanity; screenshots for David (hard rule) | manual | David confirms |
| T9 | live soak 30 min: health `vote_merges + vote_splits` vs client `rekeys`; `reid_revivals` sanity; CPU/temp unchanged; `seqGaps` unchanged | Pi + Mac Chrome | counters agree; no regressions |

---

## 8. Implementation plan (ordered; each step small, testable, deployable on its own)

| step | change | anchors | proves |
|---|---|---|---|
| S1 | Track fields; tracker `pts=` kwarg (v4 + v3), `seg_pts` at spawn/revival/merge/split, frozen-snapshot fields, `identity_log` with monotonic ids | `tracker_common.py:45-74`; `tracker_v4.py:280, 470-476, 509-521, 524-540, 586-620, 622-671`; `tracker.py:128` | T1 |
| S2 | process_thread: pass pts; set tentative/lock_pts/label_epoch at the three transitions; `_label_state`; payload fields; `_drain_identity_ops` with 10-event repeat | `process_thread.py:196, 263-283, 386-407, 422-441, 515-519` | T2 |
| S3 | `emit(identity=None, schema)`; update fakes | `sse_events.py:152-175`; `tools/demo_grade.py:55-58`; `tools/offline_replay.py:~83`; `tests/pipeline/test_process_thread.py` | T3, T4 (script) |
| S4 | Deploy server side (backward compatible — old client ignores) in the next bundled pipeline restart (mission rule: one final deploy); confirm a live SSE sample shows `schema:2` + fields; watch health CPU/temp for 10 min | rsync + `systemctl --user restart bird-pipeline` (bundled) | live sample; no CPU delta |
| S5 | Client refactor only: extract the buffer core to `dashboard/overlay-engine.js` + route; behaviour identical | `pi_dash.html:2273-2328, 2491-2525`; `api.py:465-468` | T5 (structure), existing rig run unchanged |
| S6 | Client logic: identity ops, R1–R4, render rule, CSS, diag/`__overlayDebug` fields, legacy fallback, tightened retention | `pi_dash.html:2055-2114, 2340-2388, 2442, 2491-2525, 2665-2767`; CSS `:309-356` | T5 (full), T6 |
| S7 | Delay control: `DELAY_MS`, all receivers, `mseTargetGap` patch, iPad forced-MSE, button copy; **measure achieved `jitterBufferDelay` at 1500 on Mac Chrome** (go/no-go → MSE fallback) | `pi_dash.html:1929-1950, 2194-2229, 982-983`; `video-rtc.js:478-479` | T7 |
| S8 | Rig runs at the new delay; `lab grade` census diff; iPad screenshots for David | `tools/sync_rig.py`, `tools/lab` | T4 (Pi), T7, T8 |
| S9 | Docs: spec addendum (this file → `docs/working/specs/`), book `05-dashboard.md` and `10-overlay-sync.md` addenda, CLAUDE.md "Video Path" paragraph (0.8 s → 1.5 s, verified-only rule) | — | code-to-doc check |
| S10 (optional) | SSE replay-on-connect ring (last ~D+1 s per camera) for reconnect-mid-visit | `sse_events.py:181-194` | T9 reconnect drill |

Effort [E]: S1–S3 ≈ 0.5 day; S5–S7 ≈ 1 day; S8 ≈ 0.5 day (rig ≈ 8 min/run, device check, screenshots). ≈ 2 engineer-days; the server half can ship first with zero user-visible change.

---

## 9. Risks

1. **Chrome may not honour a 1500 ms `jitterBufferTarget` fully** [A]. Sync stays exact (C uses measured buffering), coverage would drop toward the 0.8 s figures. Mitigation: measure in S7; fall back to forced MSE (deterministic) on Chrome.
2. **iPad MSE fractional `playbackRate`** is inherited from the existing law but the device confirmation is still an open item in the book. Hard gate T8.
3. **Vendored `video-rtc.js` patch** (2 lines, parameter with upstream default) must be re-applied on a go2rtc upgrade; add a comment + a test that greps for `mseTargetGap`.
4. **Contradiction unlocks are not scrubbable** at any modest D (§1c, structural ≥ 2 s). Expectation must be set: property 2 covers locks, merges and splits; contradictions (0.8 %/lock) still flip on glass.
5. **Retroactive backfill across a wrong merge** labels the young segment with the survivor's species before display. Same exposure the tracker already has; bounded by the 0.60 cosine gate and undone by split/contradiction.
6. **Glass latency rises 0.8 → 1.5 s** in Smooth mode. The owner explicitly accepted it; Realtime remains one tap away.
7. **Payload field creep**: four per-track fields + top-level list. Kept idempotent and optional; `schema` versioned.
8. **Hidden state coupling**: `tentative` must ride the frozen snapshot through loss/split (`tracker_v4.py:470-476, 656-659`) or a split can restore `is_locked` with a stale `tentative`. Covered by T1.
9. **Split newcomer never appears in `TrackerOutput.new`** (`tracker_v4.py:654` adds to `self.tracks` without `new_tracks`) — pre-existing; `event_store` never writes `is_new=1` for it. Not needed by this design (seg_pts covers birth), but worth a one-line fix while there.
10. **Lab time on the Pi**: T4 (Pi) and T7 need demo mode → production downtime ≈ 3–4 min each (mission tally already ≈ 12 min today). Schedule once, bundled.

---

## SUMMARY

- Recommended display delay **D = 1.5 s** (Smooth default; `DELAY_MS` const, URL/localStorage override; 2.0 s is a one-value change). Basis [M]: prod lock latency n=2923, p50 0.27 s / p90 0.94 s / p95 1.37 s; coverage 0.8 s 86 % → 1.5 s 95.4 % → 2.0 s 96.6 % → 3.0 s 96.9 %. Reel v4.4: 7 locks, p50 0.73 s; the titmouse label rode the jay 0.96 s before the split — fully scrubbed at 1.5 s, half-shown at 0.8 s.
- Unlocks are rare (35/3140 = 1.1 %) but late (0/35 within 1 s; floor ≈ 2 s by `LOCK_VERIFY_EVERY×LOCK_UNLOCK_N`): contradictions cannot be scrubbed by a modest delay — scrub value is locks (backfill from first frame), merges and splits.
- Schema (backward compatible): per-track `label_state {none,candidate,locked,tentative}`, `label_epoch`, `seg_pts`, `lock_pts`; top-level `schema:2` + `identity:[{id,op:merge|split,from,into|to,at_pts}]`, repeated on 10 events. Emitted at `process_thread.py:263-283` via `sse_events.emit(identity=)`; sourced from `tracker_v4.py` merge/split (`:586-671`) with a new `identity_log` and `update(..., pts=)`.
- Client (`pi_dash.html`): ops first, then samples; R1 backfill on lock/relabel, R2 strip on contradiction (not on tentative), R3 merge rekey, R4 split rekey from `at_pts` + strip; render species only for locked/tentative, unverified = box with **no text** ("identifying…" only in `?syncdiag=1`); `jitterBufferTarget=DELAY_MS` on all receivers; MSE depth via a 2-line `mseTargetGap` parameter in vendored `video-rtc.js:478-479` (D ≤ 3.5 s); iPad Safari forces MSE in Smooth mode.
- Failure modes handled by idempotent per-track state (reconnect, drops), existing reanchor/epoch logic (restart, recalibration), and measured bounds (late hints). Pi cost ≈ 0 (µs/frame, +~60 B/track/event).
- Tests: tracker/process_thread/sse unit tests; census invariance (must stay 6/0/3/0); Node `overlay-engine.js` unit tests (split 8→9 @137.07 case); replay "retractions on glass" = 0 at 1.5 s; sync rig at the new delay with unchanged gates (±40 ms / ≤8 px) + achieved-delay check; iPad screenshots for David.
- Risks: Chrome honouring 1500 ms (measure; MSE fallback), iPad MSE confirmation, vendored-player patch on upgrade, contradictions still flip on glass, +0.7 s glass latency (owner accepted). Effort ≈ 2 engineer-days; server half ships first with no visible change.
