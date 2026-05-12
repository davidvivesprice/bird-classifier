# Codex takeover control note

**Date:** 2026-05-12
**Owner:** Codex
**Branch:** `pi-main`
**Current branch head:** use `git rev-parse HEAD` in `/Users/vives/bird-classifier-pi`

## Operating rule

Use `/Users/vives/bird-classifier-pi` as the Pi-side source of truth. The Pi
runtime at `vives@pi5.local:/home/vives/bird-classifier` is a deployment target,
not the git authority. Its worktree is dirty because historical runtime files,
backups, vendored experiments, and copied source trees live there.

Do not reason from `git status` on the Pi alone. Compare deployed files to
`pi-main` when necessary, then deploy by rsyncing from this checkout.

## State recovered

- `pi-main` is the correct GitHub branch for the Pi-side repo.
- `main` is the iMac-side branch and should not receive Pi commits.
- `pi-main` already contains the overnight WebRTC + DOM label recovery work.
- Runtime-critical deployed files matched `pi-main` before the first takeover
  fix, except for non-critical docs/tests/legacy drift.
- The dashboard now has a WebSocket mirror for label events:
  `/api/pipeline/events/ws?camera=feeder`.
- `pi5.vivessato.com` uses the WebSocket label transport; LAN keeps using SSE.
- The Pi dashboard no longer depends on the dead `go2rtc.vivessato.com`
  hostname. It serves `/video-stream.js` same-origin and sends video signaling
  through the existing dashboard `/api/ws` proxy.
- `feeder-demo` is allowed through `/api/ws`, so demo mode can render remotely.

## Verified after takeover fix

- Focused TDD test on Pi venv:
  `tests/test_pipeline_events_ws.py` -> `2 passed`.
- `dashboard/api.py` compiles under the Pi venv.
- Deployed `dashboard/api.py` and `dashboard/pi_dash.html` to the Pi.
- Restarted `bird-dashboard`.
- Services active: `bird-dashboard`, `bird-pipeline`, `go2rtc`, `cloudflared`.
- Pipeline health: `overall == ok`.
- LAN SSE still works: `119` data events in 4 seconds.
- New LAN WebSocket event bridge works: received `10` live events.
- Bare unauthenticated `wss://pi5.vivessato.com/...` probe redirects to
  Cloudflare Access login. That is expected from CLI without Access cookies;
  authenticated browsers should carry the cookie on the WebSocket request.
- Focused TDD test on Pi venv:
  `tests/test_dashboard_live_video_proxy.py tests/test_pipeline_events_ws.py`
  -> `5 passed`.
- LAN Playwright reload of `http://pi5.local:8099/?syncdiag=1`:
  no console errors, `/video-stream.js` and `/video-rtc.js` served from `:8099`,
  labels visible, and browser video state reported `640x360`.
- Authenticated Chrome fresh-tab probe of
  `https://pi5.vivessato.com/?syncdiag=1&cb=20260512T0215`:
  video and labels visible together; diag showed ~30 Hz events and
  `video: 4 640x360`.
- Label toggle verified in Playwright: `Labels` hides `.live-label` nodes and
  `Labels off` restores them.

## Current known problems

1. **Some already-open Chrome/Safari tabs can stay in a bad stale render.**
   Fresh authenticated Chrome with a cache-busting query rendered correctly.
   If a tab is blank, open a new tab or hard reload with a changed query string
   before debugging server code.

2. **Snapshots are still 640x360.**
   The current substream FrameCapture keeps CPU under control, but the high-res
   review/training requirement is not restored. Do not accept "fetch current
   frame from main stream" as equivalent to a time-aligned high-res ring.

3. **Spatial subtitles remain the long-term architecture.**
   The current WebRTC + DOM overlay is the `Live Now` mode. It is useful, but it
   is not the exact delayed, classifier-aware spatial-subtitle renderer.

4. **Runtime directory needs cleanup later.**
   Do not clean it during feature work. First make a separate cleanup plan with
   a file inventory and exclusions.

## Next task order

1. Re-check remote video on Safari/iPad after a fresh load. Chrome fresh-tab
   acceptance passed; Safari's existing tab previously showed black video while
   label events were alive.
2. Restore real high-res snapshot alignment with a timestamped main-stream ring
   or equivalent time-aligned capture path.
3. Add timeline/ClockBridge diagnostics from the spatial-subtitle spec.
4. Only then start replacing the live overlay with delayed spatial subtitles.

## Acceptance-test pattern

Every task must state:

- Problem
- Evidence
- Smallest change
- Verification command or browser probe
- Rollback path
