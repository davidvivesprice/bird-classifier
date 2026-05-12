# Codex takeover control note

**Date:** 2026-05-12
**Owner:** Codex
**Branch:** `pi-main`
**Current commit:** `0aeed01`

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

## Current known problems

1. **Remote label UX needs browser verification.**
   The server bridge is verified. The true acceptance test is loading
   `https://pi5.vivessato.com/?syncdiag=1` in an authenticated browser and
   seeing the `events:` count increase with labels visible.

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

1. Browser-verify remote labels through Cloudflare Access.
2. If remote labels fail, debug WebSocket cookies/Cloudflare policy before
   touching overlay rendering.
3. Restore real high-res snapshot alignment with a timestamped main-stream ring
   or equivalent time-aligned capture path.
4. Add timeline/ClockBridge diagnostics from the spatial-subtitle spec.
5. Only then start replacing the live overlay with delayed spatial subtitles.

## Acceptance-test pattern

Every task must state:

- Problem
- Evidence
- Smallest change
- Verification command or browser probe
- Rollback path

