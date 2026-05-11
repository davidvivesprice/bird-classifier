# Codex Audit Prompts (paste-ready)

**For:** Codex running on David's MacBook Pro
**Date:** 2026-05-11
**Companion to:** `2026-05-11-pipeline-cpu-audit-plan.md`

These three prompts are mirrors of the agent prompts I (Claude on the iMac) am running in parallel. We want **two independent reads** of each track so we don't trust a single agent.

## TOP-LEVEL CONSTRAINT (applies to all three tracks)

**The system must scale to N=4-8 high-res RTSP cameras** on the same Pi 5 without breaking a sweat. The current 213% CPU at N=1 is already saturating. Every claim and every direction must be evaluated at scale.

Target budget: ≤50% CPU per camera, leaving ≥50% headroom for dashboard, snapshot writer, HLS segmenter, OS.

For each finding, also answer: **"How does this scale from N=1 to N=4?"**

---

## Setup for Codex (do once)

The codebase Codex needs to read lives on the iMac at `/Users/vives/bird-classifier-pi/`. From the MacBook Pro, you need ONE of:

- **SMB/AFP mount of the iMac**: probably mounted at something like `/Volumes/vives/bird-classifier-pi/` — check with `ls /Volumes/`
- **SSH to the iMac**: `ssh vives@imac.local` (or whatever hostname) and read from `/Users/vives/bird-classifier-pi/`
- **SSH to the Pi**: `ssh vives@pi5.local` — Pi has the deployed copy at `/home/vives/bird-classifier/`. Slightly older if the iMac hasn't rsynced lately, but functionally equivalent for reading.

Replace `${REPO}` in the prompts below with whichever absolute path Codex can actually reach (e.g., `/Volumes/vives/bird-classifier-pi` or `~/mounts/imac/bird-classifier-pi`).

For runtime checks against the Pi (codec availability, current go2rtc config, current pipeline thread state), Codex can SSH directly: `ssh vives@pi5.local "<command>"`.

---

## Codex Prompt — Track A: Codec & Decoder Audit

```
You are auditing a Raspberry Pi 5 + Hailo-8L bird classifier pipeline for unrealized hardware acceleration. The pipeline reads RTSP from a UniFi Protect camera (via local go2rtc relay), runs Hailo YOLO detection, runs an ONNX classifier on detections, writes HLS segments for browser playback, and broadcasts per-frame events via SSE.

Symptom: bird-pipeline.service uses 213% CPU on a 4-core Pi 5 and is thermally throttled at 84-85°C. Two top threads run at ~56% and ~44% of a core — strong signature of two software H.264 decoders racing.

Repo root on the machine you can read: ${REPO}
Live runtime: ssh vives@pi5.local

Your job: confirm or refute the following 9 claims with evidence (cite file:line or command output). Do NOT trust the claims.

C1. The pipeline uses 213% CPU primarily because two PyAV clients decode the same 1080p H.264 stream in software: FrameCapture decodes for Hailo input; HlsSegmenter opens its own RTSP client.

C2. PyAV does NOT use the Pi 5's hardware H.264 decoder (V4L2 M2M / `h264_v4l2m2m`) by default — it falls back to libavcodec software decode.

C3. Hailo NPU consumes near-zero CPU for detection. The classifier (AIY-ONNX) consumes some CPU only when a bird is in frame.

C7. The Pi 5 has hardware H.265 *decode* but NO hardware encode. Re-encoding H.264→H.265 on the Pi would be CPU-bound — worse than the current state.

C8. There is a real H.265 path if the UniFi camera supports H.265 output. Then Pi receives H.265, hardware-decodes it, no encode needed.

C9. Switching FrameCapture to use hardware H.264 decode via V4L2 M2M is a big unrealized win — possibly bigger than any architectural change.

Files to read (under ${REPO}):
  - pipeline/frame_capture.py — how PyAV is initialized, any hwaccel flags?
  - pipeline/hls_segmenter.py — does it actually decode, or is `add_stream_from_template` truly passthrough mux?
  - pipeline/hls_recorder.py — does this run too? competes for the same source?
  - bird_pipeline_v3.py — confirm both segmenter and frame_capture open RTSP independently
  - dashboard/api.py — locate the `/api/hls-live/{camera}` route around line 282

Runtime checks (via ssh vives@pi5.local):
  - cat ~/.config/go2rtc/go2rtc.yaml — what codec does the camera output? what transcoding is configured?
  - v4l2-ctl --list-devices — what video codec devices exist?
  - ls /dev/video* — confirm M2M devices
  - python3 -c "import av; print([c for c in av.codecs_available if 'h264' in c or 'hevc' in c])" — what codecs are available in the installed PyAV?
  - python3 -c "import av; c = av.codec.Codec('h264_v4l2m2m', 'r'); print(c.long_name, c.type)" — confirm hardware decoder accessible
  - For the camera: log into UniFi Protect (or read its API) — does the camera support H.265 output? G4 Pro and similar models do.

Output format:
  Findings (one per claim):
    C1: AGREE / DISAGREE / NEEDS_EVIDENCE — <evidence with file:line or command output>
    C2: ...
    ...

  Concrete change list (ranked by expected CPU savings, highest first):
    1. <change description> — files: <list> — expected savings: <%> — rationale: <why>
    2. ...

  Open questions for human follow-up: <list>
```

---

## Codex Prompt — Track B: Pipeline Architecture & Hidden Cost Audit

```
You are auditing a Python multi-threaded video pipeline for accidental inefficiency, hidden allocations, and design smells. The pipeline runs on a Raspberry Pi 5 and is currently using 213% CPU. We've already identified the two PyAV decoders as the dominant cost. Your job is to find what else is wrong.

Repo root: ${REPO}
Live runtime: ssh vives@pi5.local — pipeline PID at time of audit: pgrep -f bird_pipeline_v3

Your job: find inefficiencies that are NOT the two decoders. Catalog them with file:line references and rank by expected CPU impact.

Specifically, hunt for:
1. Per-frame heap allocations that could be pooled (numpy arrays, av.VideoFrame objects, JPEG buffers)
2. Redundant color-space conversions (e.g., YUV→BGR→RGB)
3. Software resizes that the GPU/ISP could do
4. time.sleep() calls that mask design issues (busy-wait avoidance can hide bugs)
5. Unbounded queues missing backpressure
6. Per-frame JSON serialization or deepcopies for SSE
7. Redundant work — anything done every frame that could be done every Nth frame
8. GIL contention — multiple Python threads doing CPU-heavy work without releasing the GIL
9. Hidden per-frame logging
10. Memory leaks (rss growth over time)
11. Thread starvation (threads pinned to overloaded cores)
12. Confirm or refute: 24%-each thread tier is classifier + YOLO postprocess + snapshot encode

Files to read (under ${REPO}):
  - pipeline/frame_capture.py — full file
  - pipeline/process_thread.py — full file (per-frame work happens here)
  - pipeline/classifier.py, pipeline/pi_classifier.py, pipeline/hailo_classifier.py — which path is active?
  - pipeline/hailo_detector.py — Hailo dispatch overhead per frame
  - pipeline/tracker.py — per-frame state cost
  - pipeline/snapshot_writer.py — encode cost and frequency
  - pipeline/sse_events.py — serialization cost per event
  - bird_pipeline_v3.py — thread setup, queue sizes, sleep behavior

Runtime checks (via ssh vives@pi5.local):
  - top -b -n 5 -H -p $(pgrep -f bird_pipeline_v3) — per-thread CPU sample
  - cat /proc/$(pgrep -f bird_pipeline_v3)/status | grep -E '^(VmRSS|Threads|voluntary|nonvoluntary)' — context switches and memory
  - Sample twice 30s apart to compute deltas — look for high involuntary context switches (= GIL contention or scheduler thrash)

Output format:
  Inefficiencies found (ranked by expected impact):
    1. <description> — file:line — impact: HIGH/MED/LOW — fix: <one-sentence>
    2. ...

  Confirmation of expected costs:
    - 24%-tier thread breakdown: <your finding with evidence>

  Bugs found (anything that's not just inefficient but actually wrong):
    1. ...

  Open questions: <list>
```

---

## Codex Prompt — Track C: Architectural Alternatives Audit

```
You are reviewing an architectural decision document for a live-video + label-overlay system on a Raspberry Pi 5. The author (another AI agent) has proposed 4 directions and is recommending one. Your job is adversarial review — find the holes.

Repo root: ${REPO}

Read these files in order:
  1. ${REPO}/docs/working/progress/2026-05-10-overlay-sync-handoff.md — full session context
  2. ${REPO}/docs/working/specs/2026-05-10-pi-overlay-sync-bedrock-design.md — the original spec (3 audit passes; ALSO failed under load)
  3. ${REPO}/docs/working/plans/2026-05-11-pipeline-cpu-audit-plan.md — the document under review. Pay particular attention to claims C1-C14 and directions D1-D4.
  4. ${REPO}/dashboard/pi_dash.html — the current setupLiveView() (HLS+canvas, currently failing under load)
  5. ${REPO}/pipeline/hls_segmenter.py — the segmenter falling behind
  6. ${REPO}/bird_pipeline_v3.py — pipeline orchestration

Reference for git history on the working WebRTC version:
  cd ${REPO} && git log --oneline | head -30
  Working WebRTC + DOM-overlay state existed BEFORE commit 1503435. Cherry-pick or use git show to see what was there.

Your job:
1. For each claim C1-C14 in the audit plan: AGREE / DISAGREE / NEEDS_EVIDENCE with reasoning.
2. For each direction D1-D4: estimate complexity, identify hidden assumptions, name failure modes the author has NOT named.
3. Specifically challenge D1's central assumption: "live view doesn't need frame-accurate sync." For a backyard bird observatory where birds move fast (jumping, flying away), is sub-second skew acceptable, or will labels visibly trail birds in a way that looks broken?
4. Specifically challenge D4's central assumption: "if the foundation is fixed, the architecture is fine." Are there latent problems in the current architecture that won't be solved by hardware-decode + single-shared-decoder?
5. Identify any D5+ direction the author has missed. Examples to consider: server-side compositor renders labels INTO the video (D2 variant); offload encode to a USB hardware encoder; use a different camera that outputs JSON metadata + H.265.
6. Argue: which direction is the smallest change that gets labels on birds TONIGHT? Which direction is the smallest change that's also bedrock for the next 18 months? Are they the same?

Output format:
  Claim review (C1-C14):
    C1: AGREE — <reason>
    ...
    C14: NEEDS_EVIDENCE — <what evidence would settle it>

  Direction review (D1-D4):
    D1: complexity estimate <S/M/L>; hidden assumptions: <list>; failure modes: <list>
    ...

  Direction missed (D5+):
    <description, why it might beat D1-D4>

  Recommendation:
    Tonight (working overlay before David sleeps): <direction> because <reason>
    18-month bedrock: <direction> because <reason>
    Are they the same? <yes/no, why>

  Confidence: <how confident you are in this review>
```

---

## How to use this

1. Mount the iMac repo on your MBP (or use SSH).
2. Substitute `${REPO}` in each prompt with the path Codex can reach.
3. Open three Codex sessions, paste one prompt in each. Let them run in parallel.
4. When done, paste their findings back to me (Claude on iMac) and I'll synthesize against my own three agents' findings.

The two-independent-reads pattern means we trust the conclusion only when Claude-agents and Codex-agents agree. Disagreements get a third investigation.
