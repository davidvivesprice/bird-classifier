# Overlay sync — session handoff (2026-05-10, late)

**Read this entire doc before doing anything. Do NOT dive into a fix. Ask David for direction first.**

## One-paragraph reset

Earlier today we had working overlays on the dashboard (WebRTC video + DOM-based bbox/label divs with CSS transitions — visible on Mac and iPad). Through the day we replaced that with a "bedrock" HLS + canvas + PTS-sync browser stack. The new stack is architecturally cleaner and the server-side work is real (single-stream pipeline, snapshot accuracy, PTS clock) — but **the browser-side overlay is currently less reliable than what we had this morning**. David is frustrated by the whackamole loop and asked the session be compacted. He is **not in agreement with a full revert**; he wants forward motion but tighter and more empirical, not screenshot-back-and-forth.

## What is verified bedrock (DO NOT regress)

These are confirmed by command (curl, ffprobe, pytest), not by screenshot:

- **Single-stream PyAV pipeline** at `pipeline/frame_capture.py` — decodes 1920×1080 from feeder-main, downscales to 640×360 in-process, attaches PTS to every `Frame`.  Verified: `journalctl --user -u bird-pipeline.service | grep "PyAV stream open"`.
- **Snapshots written from same decoded buffer** (`pipeline/snapshot_writer.py`) — the "bracket-on-empty-feeder" bug from start of session is dead. Snapshots show the bird that was detected.
- **SSE events carry `pts` field** (`pipeline/sse_events.py`). Verified by `curl http://localhost:8099/api/pipeline/events/sse?camera=feeder` on Pi LAN: ~5–30 Hz, well-formed JSON.
- **HLS segmenter + sidecar** write segments + manifest + `segments.json` to `~/bird-snapshots/hls/feeder/`. Verified: 15/15 unit tests pass (`venv/bin/python3 -m pytest tests/pipeline/test_hls_segmenter.py`).
- **HLS prototype byte-exact PTS preservation** via `add_stream_from_template` (commits `ac77abc`, `6c873cc`).
- **Annotation parser** at `tools/annotation_parser.py` (commits `a941037`, `b8dae1b`).
- **Matcher + harness** at `tools/sync_matcher.py`, `tools/sync_replay_assert.py` (commits `e3743b9`, `8d36bb8`).
- **Production sentinel** at `/api/overlay-sync-health` (commit `f114987`).

## What is NOT working

- **HLS segmenter falls behind FrameCapture under demo-mode load.** After 11 minutes the segmenter is 5 minutes behind. Two independent PyAV decoders competing with Hailo + classifier on the Pi 5 → segmenter runs at ~56% of realtime. Evidence: debug hook showed `framePts=364, eventBuf_last_pts=673, gap_to_frame=-308s`. The drift grows monotonically.
- **Browser canvas overlay rarely renders.** Caused by the above — `trackHistory` accumulates events for PTS far ahead of any frame currently being played, so renderAt's stale check (`framePts > lastPts + STALE_S + FADE_OUT_S`) skips every track.
- **Cloudflare tunnel does NOT pass SSE** through. Pi LAN: 84 events in 4s. Tunnel: 0 events in 8s. Confirmed by curl.
- **Demo mode writes into production DB / snapshot directories.** Sandbox separation not yet built. David explicitly asked for this earlier.
- **Demo video on Pi was re-encoded to consistent 2s GOPs** (`~/bird-snapshots/demo/may10_demo_normalized.mp4`, ~69 MB). Demo-loop service updated to point at it. Visual artifacting should be gone, but the segmenter-drift problem still occurs because of CPU contention, not GOP shape.

## What I broke (and how it happened)

This morning the dashboard had a working WebRTC + DOM-label overlay. Through the bedrock rewrite (Phase B) we replaced it with `<video>` + hls.js + canvas + PTS-sync. The new stack has multiple load-bearing assumptions that don't survive demo-mode load. **The previous WebRTC+DOM overlay is still recoverable from git** (the most recent commit before this session was the working state, plus several iterations during this session that worked at various stages).

## Failure patterns I've fallen into this session

Post-compact me: read these and avoid them.

1. **Symptom-chasing**: David reports "no labels on Mac" → I instantly start fixing labels-on-Mac instead of asking what the broader goal is. Symptoms are data, not direction.
2. **Talking too much**: long bulleted responses. David has explicitly said this is exhausting. **Keep responses short.**
3. **Whackamole**: bug → fix → didn't help → next bug → fix → didn't help. Without a coherent plan I'm just dancing.
4. **Restarts during diagnosis**: I restart services repeatedly, causing transient 502s on David's iPad, then chase those 502s instead of the actual problem.
5. **Assuming Chrome MCP works for visual testing**: it doesn't — its tabs run hidden, MediaSource refuses to open, no rendering happens. **Use Playwright headed via `venv/bin/python3 /tmp/dash_probe.py URL OUT WAIT_S`** instead. That's a real visible browser on the iMac.
6. **Trusting state right after restart**: things look good for ~30 seconds then drift. Always wait at least 60s before sampling.

## Open options David has NOT yet chosen between

Earlier in the session I proposed (and David has been silent on):

- **Option 1: Single shared PyAV decoder.** FrameCapture demuxes packets, forwards via a bounded queue to the segmenter which only does mpegts mux. Eliminates the two-consumer CPU competition. ~150 lines server change.
- **Option 2: Use go2rtc's built-in HLS output.** Lose our PTS-in-sidecar mechanism; re-engineer PTS carrier via SSE wall-clock anchor. Bigger architectural change.
- **Option 3: Bandaid — segmenter drops frames to catch up.** Will keep drifting under load. Not bedrock.

David's last directive: *NOT* in agreement with a full revert (Option 4 I'd proposed). He wants forward motion, tighter and more empirical.

## Critical files

- `dashboard/pi_dash.html` — **HAS DAVID'S PARALLEL THEME WORK. Do NOT touch theme CSS, theme switcher JS, or panels other than the live-stage region.** The bedrock-overlay JS lives inside `function setupLiveView()`.
- `pipeline/hls_segmenter.py` — segmenter class. No PDT emission (we removed it because hls.js misinterprets 1970-epoch PDT as wall-clock and stalls live-edge logic). Uses `add_stream_from_template` for PTS-preserving mpegts mux.
- `bird_pipeline_v3.py` — pipeline orchestrator. Sets up FrameCapture AND HlsSegmenter both reading `rtsp://localhost:8554/feeder-main` (live) or `rtsp://localhost:8654/feeder-main` (demo mode via `PIPELINE_TEST_RTSP_URL`).
- `~/bird-classifier/scripts/demo-loop.sh` on Pi (**not in repo**) — mediamtx + ffmpeg-stream-loop for demo. Currently points at `~/bird-snapshots/demo/may10_demo_normalized.mp4`. David built this himself and added a `/api/demo-mode` endpoint to `dashboard/api.py` to toggle it.

## State of the Pi RIGHT NOW

```
demo mode: ON  (PIPELINE_TEST_RTSP_URL=rtsp://localhost:8654/feeder-main set)
PIPELINE_NIGHT_BYPASS=1 set (so capture doesn't pause for nighttime — it's late evening)
bird-pipeline.service: active
bird-demo-loop.service: active
bird-dashboard.service: active
```

To return to live UniFi mode:
```bash
ssh vives@pi5.local "curl -s -X POST -H 'Content-Type: application/json' -d '{\"enabled\": false}' http://localhost:8099/api/demo-mode"
```

To unset night bypass (so capture pauses at night again):
```bash
ssh vives@pi5.local "systemctl --user unset-environment PIPELINE_NIGHT_BYPASS && systemctl --user restart bird-pipeline.service"
```

## Empirical verification tools

- **`/tmp/dash_probe.py`** — Playwright-driven probe. Visible Chromium on iMac. Run: `venv/bin/python3 /tmp/dash_probe.py http://pi5.local:8099/?syncdiag=1 /tmp/out.png 45`. Loads page, waits N seconds with per-5s progress ticks, dumps `window.__overlayDebug` JSON, screenshots. **Use this instead of asking David for screenshots.**
- **Debug hook in `pi_dash.html`** — `window.__overlayDebug` is populated each renderAt frame. Contains per-track PTS ranges, current fragment, sidecar window, video.currentTime. Inspect via Playwright eval.
- **Pi-side checks** (run via ssh):
  - Sidecar window: `cat ~/bird-snapshots/hls/feeder/segments.json | python3 -c "..."` — shows first/last pts_start, segment count
  - SSE flow: `timeout 4 curl -N -s 'http://localhost:8099/api/pipeline/events/sse?camera=feeder' | grep -c data:`
  - Tunnel SSE flow: `timeout 8 curl -N -s 'https://pi5.vivessato.com/api/pipeline/events/sse?camera=feeder' | grep -c data:` — currently returns 0 (Cloudflare buffers SSE)
  - Sentinel: `curl -s http://localhost:8099/api/overlay-sync-health | python3 -m json.tool`

## Recent commits this session

- `92dd6a2` server-side single-stream + PTS clock
- `a941037`/`b8dae1b` annotation parser
- `f76e8d6`/`08e02c1`/`b3805d7`/`7d62d46`/`686ca3c` segmenter incremental
- `6912b09` pipeline integration of HlsSegmenter
- `96ab383` vendor hls.js
- `1503435`/`e710afa`/`6b0f28b`/`d114921`/`6eaca22` browser overlay rewrite (the failure-prone part)
- `c12f82c` browser fix attempts (canPlayType ordering, PDT removal, sidecar unification)
- `f114987` overlay-sync sentinel
- `bf90079` runbook

The "good working state on Mac" we had this morning is at git history BEFORE commit `1503435` (which replaced video-stream with vanilla video).

## Reading order for post-compact Claude

1. This doc, top to bottom.
2. `docs/working/specs/2026-05-10-pi-overlay-sync-bedrock-design.md` — the spec (3 audit passes).
3. `docs/working/plans/2026-05-10-pi-overlay-sync-bedrock.md` — the implementation plan.
4. `dashboard/pi_dash.html` lines 1212–1640 — current setupLiveView (NOT WORKING).
5. `pipeline/hls_segmenter.py` — the segmenter that falls behind under demo load.

## What David explicitly wants

From his last message before compact:

> "What are the tools you have? You have a million skills, right? What are the tools you have to not have to just kind of be like 'oh well it's not working so let's back up'... What are the things that are actually bedrock? Can we please not get so lost trying to fix one thing that we literally already had... You're not utilizing your tools. You're forcing me to be the one that thinks about the tools. But you're the expert."

He also said: keep responses **short**. Be empirical. Use Playwright assertions, not screenshots. Identify what we ACTUALLY rely on (bedrock) and protect it.

## First message to David after compact

**Do not start coding.** Acknowledge this handoff doc was read. Ask David which of the 3 options he wants, OR if he wants to talk through them more first. Keep that message to 5 lines or fewer.
