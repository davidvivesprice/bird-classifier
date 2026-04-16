# Overlay Sync — Ground-Truth Verification Design

**Date:** 2026-04-16
**Status:** Verification phase — no production changes authorized
**Successor to:** `2026-04-15-delayed-playback-overlay-design.md`

## 1. Problem statement

The delayed-playback `/live` page draws bounding boxes roughly **one second ahead** of the bird visible in the video. The SSE-event-to-`playingDate` match distance measures at ~80 ms, so the timestamp-matching math is correct. The offset lives in the stamping itself: what the pipeline calls "wall_time_ms" and what HLS calls `#EXT-X-PROGRAM-DATE-TIME` do not refer to the same moment in physical time.

A constant client-side compensation has been added as a stopgap (`OVERLAY_LEAD_COMPENSATION_MS` in `dashboard/live.html`, default 1000). That is explicitly a band-aid. This spec defines the verification work required before we replace the band-aid with a drift-proof fix.

## 2. Success criterion for this verification phase

A written record (this document, updated in place) in which each of four gates below has exactly one of the following statuses, with cited evidence:

- `[VERIFIED-DOCS]` — confirmed by reading authoritative documentation
- `[VERIFIED-CODE]` — confirmed by reading the actual source code we execute
- `[VERIFIED-TEST]` — confirmed by a repeatable measurement on this system
- `[INCONCLUSIVE]` — we do not yet know; the doc states what would make it conclusive

No gate concludes "probably yes." If we cannot verify, we say so and stop.

## 3. Non-goals of this phase

- No edits to `pipeline/frame_capture.py`, `pipeline/hls_recorder.py`, `dashboard/live.html`, or `dashboard/hls-test.html`.
- No restart of `bird_pipeline_v3`, `hls_recorder`, or any ffmpeg subprocess.
- No ffmpeg flag changes on running processes.
- No implementation plan — that comes after this doc is reviewed and approved, via the `writing-plans` skill.

## 4. Gates

### Gate 1 — Clock ground truth

**Question:** Do the iMac, CloudKey, and UniFi cameras agree on what time it is, within a usable tolerance?

**Why it matters:** Every downstream timestamp is expressed relative to some clock. If the underlying clocks disagree by more than a few tens of milliseconds, no amount of timestamp plumbing produces a pinned-to-truth overlay. This gate sets the noise floor for every other gate.

**Method:**
- `ntpq -p` on the iMac — record stratum, source, offset, jitter.
- CloudKey NTP status via its admin page or `ssh`.
- Camera NTP source via the UniFi Protect API (`GET /proxy/protect/api/cameras/{id}`, look for NTP fields).

**Pass criterion:** all three devices sync to the same source (or to sources traceable to the same stratum-1), with measured offsets < 50 ms.

**Fail criterion:** any device unsynchronized, or measured offset > 100 ms. If this fails, the recommendation is to fix NTP before anything else.

**Status:** _[pending]_

---

### Gate 2 — ffmpeg PDT semantics

**Question:** Does ffmpeg's `-hls_flags program_date_time` anchor the `#EXT-X-PROGRAM-DATE-TIME` value to the source RTP timestamp (mapped via RTCP Sender Report to camera NTP), or does it stamp system wall-clock time at the moment ffmpeg writes a segment to disk?

**Why it matters:** This single question decides the scope of the eventual fix.
- If PDT is RTP-anchored, the HLS side is already honest, and only `frame_capture.py` needs to change. Small surgery.
- If PDT is ingest-wall-clock-anchored, both sides are stamping dishonestly, and the fix must replace at least one of the two ffmpeg subprocesses.

**Method A — read the source (authoritative):**
- Locate ffmpeg source for this Homebrew install. If unavailable locally, fetch the matching tag from the ffmpeg git mirror.
- In `libavformat/hlsenc.c`, find where the `EXT-X-PROGRAM-DATE-TIME` line is emitted.
- Trace backward to identify the timestamp source: is it a `pkt->pts` / `pkt->dts` mapped through a demuxer-provided `start_time_realtime`, or is it `av_gettime()` / `time(NULL)` called at write?

**Method B — measure (empirical cross-check):**
- Display a millisecond-resolution clock face on a second monitor, place it in the feeder camera's field of view.
- Record for 30 s.
- Extract an HLS frame using `ffmpeg -ss <t> -i live.m3u8 -frames:v 1 out.png`. Read the clock face in the frame.
- Read the PDT of the segment containing that frame, interpolated to the frame's offset within the segment.
- Compute `pdt_frame - clock_face_reading`. Repeat 5× across different segments.

**Pass criterion:** `pdt_frame - clock_face_reading` is constant and bounded by Gate 1's noise floor.

**Fail criterion:** the delta correlates with segment position (suggesting segment-boundary stamping) or exceeds Gate 1's noise floor by more than ~200 ms.

**Status:** _[pending — run first]_

---

### Gate 3 — go2rtc RTCP exposure

**Question:** Does go2rtc expose the RTP-to-NTP mapping from RTCP Sender Reports through its HTTP API, such that a Python client could ask "what NTP time corresponds to the current RTP timestamp on the feeder stream"?

**Why it matters:** If yes, we can compute truth once in Python and distribute it, without having to parse RTCP ourselves. This is especially relevant if Gate 2 fails and we need option (b) or (c) from the decision tree.

**Method A:**
- `curl http://192.168.4.200:1984/api/streams?src=feeder` and dump all fields.
- Walk other go2rtc endpoints (`/api`, `/api/ffmpeg`, `/api/info`) and record what timing-related data is exposed.

**Method B:**
- Read go2rtc source on GitHub (`AlexxIT/go2rtc`, current release tag) for RTCP SR handling. Identify whether the NTP mapping is retained in memory and whether it's reachable from any HTTP endpoint.

**Pass criterion:** at least one API surface returns a structure from which RTP→NTP can be computed.

**Fail criterion:** go2rtc parses RTCP but does not expose it; we'd need to patch go2rtc or do our own RTCP parsing.

**Status:** _[pending]_

---

### Gate 4 — PyAV feasibility

**Question:** Can PyAV on this iMac decode the feeder substream RTSP URL and produce per-frame PTS values that we can map to wall-clock time?

**Why it matters:** The small-surgery path (rewriting `frame_capture.py`) depends on PyAV. If it doesn't work here, we either compile it from source, find a different library, or fall back to a sidecar `ffmpeg -progress` approach.

**Method:**
- `pip show av` in the pipeline's active venv. If present, proceed. If not, install in a scratch venv to avoid perturbing production.
- Write a ~30-line script that opens the substream RTSP URL, decodes 100 video frames, and prints `(frame.pts, frame.time_base, stream.start_time, computed_wallclock)`.
- Verify: PTS values are monotonic; time_base is the expected 1/90000 for RTP video; computed wall-clock is plausible.

**Pass criterion:** all 100 frames decode with monotonic PTS and a derivable wall-clock that agrees with `time.time()` to within ~1s (we don't expect it to match exactly — that's the whole reason we're investigating).

**Fail criterion:** install fails, decode fails, PTS non-monotonic, or PTS values reset unpredictably.

**Status:** _[pending]_

## 5. Gate execution order

1. **Gate 2 first.** It's the one that can invalidate the premise of the other gates. If Gate 2 fails, we pause, report, and re-evaluate scope with the user before running 3 and 4.
2. **Gate 1 always runs** regardless of Gate 2's outcome — it's the noise floor for every timing measurement, and a FAIL here blocks all downstream work.
3. **Gates 1, 3, 4 in parallel** once Gate 2's answer is in hand. They don't depend on each other.

## 6. Decision tree after all gates complete

```
Gate 1 = FAIL
  └── STOP. Fix NTP. Rerun Gate 1.

Gate 1 = PASS
  ├── Gate 2 = PASS (PDT is RTP-anchored)
  │     └── Small surgery: rewrite frame_capture.py only.
  │         Gate 4 = PASS → use PyAV.
  │         Gate 4 = FAIL → sidecar ffmpeg -progress, merge in Python.
  │
  └── Gate 2 = FAIL (PDT is ingest-anchored)
        └── Per user-approved ranking (correctness-first):
            (a) Replace hls_recorder.py with a Python muxer that stamps
                PDT from RTP timestamps directly.
                Requires Gate 4 PASS.
            (b) Switch to go2rtc native HLS output, if it preserves RTP timing.
                Requires Gate 3 PASS with demonstrable RTP-anchored PDT.
            (c) Keep both ffmpegs. Publish a master-clock translation layer
                that converts PDT to true capture time.
                Requires Gate 3 PASS at minimum.
```

## 7. Outputs

1. This document, with each gate's status filled in and evidence cited.
2. A single "Recommendation" section at the bottom of this doc naming the chosen surgery path.
3. If any gate ends `[INCONCLUSIVE]`, we stop and the doc explicitly names what would be needed to unblock.

## 8. Handoff

After this document is complete and the user has reviewed it, the next step is to invoke the `writing-plans` skill with the chosen surgery path. The implementation plan will cite this document for every claim it rests on.

## 9. Evidence log (to be filled during execution)

### Gate 1 evidence
_[pending]_

### Gate 2 evidence
_[pending]_

### Gate 3 evidence
_[pending]_

### Gate 4 evidence
_[pending]_

## 10. Recommendation

_[to be written after all gates complete]_
