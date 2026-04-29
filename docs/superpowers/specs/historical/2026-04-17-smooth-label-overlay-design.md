> **HISTORICAL** — This document describes a design, plan, or system state that has been superseded. Kept for decision-trail context.

# Smooth Label Overlay — Design

**Status:** approved 2026-04-17
**Scope:** `dashboard/live.html` only. `dashboard/sync-test.html` keeps its plain bbox as a diagnostic reference.

## Problem

The `/live` overlay matches each displayed frame to the closest SSE detection event and draws the bbox at that event's coordinates. Detections arrive at ~5fps, the video plays at ~30fps, so the box jumps in discrete ~200ms steps while the bird moves continuously. This reads as drift even though our sync math is correct (verified April 17 via `/sync-test` with both streams pinned to a shared `displayedWallMs` target — boxes drift identically on both, confirming the gap is detection cadence not sync).

## Approach: Catmull-Rom through bracketing events

Unlike real-time trackers (SORT/Kalman), we have a ~10s HLS playback buffer. When rendering a frame at wall-clock T, detection events from *both before and after* T are already in our SSE buffer. We can *interpolate* instead of extrapolate.

For each active track, at render time T:

1. Find the 4 bracketing events: `P₀, P₁` with `wall_time_ms ≤ T` and `P₂, P₃` with `wall_time_ms > T` (latest two before, earliest two after, by wall-clock).
2. Compute parameter `t = (T − P₁.wall_time_ms) / (P₂.wall_time_ms − P₁.wall_time_ms)` ∈ [0, 1].
3. Apply Catmull-Rom (centripetal variant to avoid overshoots) to both:
   - bbox center `(cx, cy)`
   - bbox dimensions `(w, h)`
4. Render at 60fps via `requestVideoFrameCallback`.

Centripetal Catmull-Rom passes through every control point — the box is at the exact detected position at every event — and is C¹ continuous, so velocity looks physically plausible.

## Edge cases

| Condition | Behavior |
|---|---|
| 4+ bracketing events available | Catmull-Rom |
| 3 events (missing P₀ or P₃) | Linear between P₁ and P₂, tangent taken from available neighbor |
| 2 events (have P₁ and P₂ only) | Linear interpolation |
| 1 event (just appeared or tail) | Hold at that position |
| T past the last known event | Hold at last position for `FADE_OUT_MS` (no extrapolation — prevents off-screen drift) |

Never extrapolate past the future horizon. If the spline would run off, we hold.

## Visual shape: corner brackets + label

Replaces the current full rectangle.

- **4 L-shaped brackets** at the smoothed bbox corners.
- **L-length** = `clamp(0.12 × min(bbox_w, bbox_h), 10px, 28px)`.
- **Stroke** = 2px, same white + drop-shadow treatment as today's label.
- **Bbox inflation** = 10% on each side before drawing brackets. Hides smoothing jitter, lets the bird breathe within the frame.
- **Label** anchored above smoothed top-edge midpoint, existing rounded-pill style, existing fade-in/out timing (200ms / 400ms).

## Files

- `dashboard/live.html` — replace `renderFrame` bbox-drawing block. Add Catmull-Rom math helpers. Swap event-buffer lookup from "closest single event" to "four bracketing events."

No backend changes. No SSE event format changes. No new dependencies.

## Rollback

Single-commit change. Revert the commit to restore current behavior.

## Out of scope

- `dashboard/sync-test.html` — stays as-is, diagnostic reference.
- Per-track species color coding — separate ask.
- Kalman filter fallback — not needed; we have the future buffer.
- Extrapolation past last event — intentionally excluded.

## Lesson carried forward

Per `feedback_read_ffmpeg_filter_semantics.md`: calibrate spec rigor to the actual question. This is a ~200-line JS change to one file. No adversarial review. Write, build, ship, verify on real birds.