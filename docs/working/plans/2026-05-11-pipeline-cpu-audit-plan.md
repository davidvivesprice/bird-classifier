# Pipeline CPU Audit & Bedrock Validation Plan

**Date:** 2026-05-11
**Author:** Claude (iMac)
**Status:** Draft — to be challenged by parallel agents AND by Codex on David's MBP before any code change

---

## Goal

Find out **what is actually consuming 213% CPU in `bird-pipeline.service`** on the Pi 5, separate genuine bedrock cost from accidental inefficiency, and produce a defensible architectural direction before writing any code. Do this by **challenging my own claims** with parallel adversarial reviewers — not by trusting my reading of the system.

This plan is the artifact under review. The agents' job is to break it.

## TOP-LEVEL CONSTRAINT: Scalability is bedrock

Added 2026-05-11 by David: **the Pi must scale to multiple cameras (target N=4-8 high-res streams) without breaking a sweat.** Original system architecture assumed multi-camera ingestion. Current design at N=1 already saturates the chip. Any direction we pick must answer:

- **What does this cost per camera?**
- **At N=4, do we still fit in the CPU + thermal budget?**
- **At N=8, what would have to change?**

A direction that works at N=1 but doesn't scale to N=4 is **not bedrock**. Agents must evaluate every claim and every direction (D1-D4) at scale, not just at the current N=1 demo configuration. If a direction only works at N=1 it must be explicitly flagged.

Order-of-magnitude math:
- 4-core Pi 5 nominal: 400% CPU. With thermal cap: effective ~300-350%.
- Current pipeline at N=1: 213% — so we'd hit the ceiling at N=2.
- Target: ≤50% CPU per camera, leaving ≥50% headroom for dashboard, snapshot writer, segmenter, OS overhead.

---

## Why this plan exists

David's stipulation for the overlay-sync design was "immune to added load." The design failed that stipulation because the validation gates were correctness-shaped, not load-shaped. We're now in a position where:

- Pipeline process is at **213% CPU** (>2 cores worth) on a 4-core Pi 5
- Chip is **thermally throttled** (`0xe0008`: ARM freq capped + soft temp limit hit) at 84-85°C
- Two top threads ~56% and ~44% of a core each — the signature of two PyAV decoders racing
- HlsSegmenter falls behind FrameCapture by ~5 min after 11 min uptime under demo load
- Browser overlay therefore renders nothing — events accumulate for PTS far ahead of any frame that ever plays

Before picking Option 1 (single shared decoder), Option 2 (go2rtc HLS), or Option 3 (drop frames), I need to be sure I understand WHY the chip is straining. My instinct says "two software H.264 decoders of a 1080p stream." David's instinct says "we're asking it to do things it shouldn't be doing — why aren't we using hardware H.265 end-to-end?" Both deserve adversarial scrutiny.

---

## Methodology: Foundations-Up Validation

Four meta-rules (established earlier this session):

1. Every step has a **measured** gate. No belief, no in-isolation prototype.
2. Every gate runs with **realistic adjacent load**.
3. A failed gate **halts forward motion** — fix the foundation or revise the design.
4. Every gate becomes a **committed script** that catches regressions automatically.

The six ordered questions (full version in earlier session):
- Q1: Load envelope — **DONE for current state. Numbers below.**
- Q2: Can foundation sustain realtime? — **OPEN. This audit answers it.**
- Q3–Q6: Defer until Q1/Q2 are green.

---

## What Q1 told us (data, not opinion)

Measured 2026-05-11 ~00:03 ET on Pi 5, demo mode, full pipeline live, dashboard active:

| Metric | Value | Note |
|---|---|---|
| `bird-pipeline` total CPU | **213%** | >2 cores worth, 42 threads |
| System CPU usage | **~54% us+sy** | ~2.16 cores busy out of 4 |
| System CPU idle | **~46%** | ~1.84 cores idle |
| Load average (1m) | **4.71** | Runqueue saturated (>4 cores) |
| Memory used | 1280 MB / 4049 MB | Healthy |
| Temp (sustained) | **84-85.6°C** | Pi 5 throttle threshold = 85°C |
| `vcgencmd get_throttled` | **`0xe0008`** | Bit 3 set: soft temp limit hit; historical bits 17/18/19 |
| Top 2 threads | **~56% + ~44% of a core** | Strong signature of two PyAV decoders |
| Next 3 threads | ~24% × 3 | Classifier + detect postprocess + snapshot encode (suspected) |
| Demo loop (mediamtx + ffmpeg) | 2.6% + 1.2% = **~4%** | Negligible; moving off-Pi won't fix anything CPU-wise |
| Other processes | uvicorn 11%, go2rtc 1.3%, cloudflared 0.7% | ~17% combined |

Additional signal: mediamtx logs "RTP packets lost" on the pipeline's consumer session — consistent with segmenter back-pressuring under contention.

---

## My current claims (to be tested)

These are my interpretations. Each one is a target for the adversarial agents:

**C1.** The pipeline is at 213% CPU primarily because **two PyAV clients decode the same 1080p H.264 stream in software**. FrameCapture decodes for Hailo input; HlsSegmenter opens its own RTSP client. Both pay full software H.264 decode cost at 1080p30.

**C2.** PyAV does **not** use the Pi 5's hardware H.264 decoder (V4L2 M2M / `h264_v4l2m2m`) by default — it falls back to libavcodec software decode.

**C3.** **Hailo NPU consumes near-zero CPU** for detection. The classifier (AIY-ONNX) consumes some CPU but only when a bird is in frame.

**C4.** The thermal throttle is a **consequence**, not a root cause. If we eliminate redundant decode work, thermals drop below the throttle threshold even with the current passive cooling. (Fan should still be fixed independently.)

**C5.** The 24%-each thread tier is likely: AIY-ONNX classifier per detection + YOLO postprocess + snapshot JPEG encode. Not the bottleneck.

**C6.** **Option 1** (single shared decoder, FrameCapture demuxes once and forwards packets to segmenter via a bounded in-process queue) eliminates one PyAV client and roughly halves the decode cost. ~150 lines of server change.

**C7.** **David's H.265 idea has a flaw on the Pi 5**: the Pi 5's VideoCore VII has hardware H.265 *decode* but **no hardware encode**. Re-encoding H.264→H.265 on the Pi would be CPU-bound — worse than the current state.

**C8.** **There is a real H.265 path** if the UniFi camera supports H.265 output. Then Pi receives H.265, hardware-decodes it, no encode needed. This is the right shape of David's idea, just at a different point in the pipeline.

**C9.** Even without H.265, switching FrameCapture to use **hardware H.264 decode via V4L2 M2M** is a big unrealized win — possibly bigger than Option 1's architectural change.

---

## My brainstormed UX/architectural directions (C10-C14, also under review)

Added 2026-05-11 after David asked me to brainstorm directions, not just diagnose. These are PROPOSALS, not conclusions. Agents should challenge them as hard as they challenge C1-C9.

**C10 — D1 (recommended): Split live view from frame-accurate replay.**
- Live = go2rtc WebRTC + canvas overlay + SSE event stream + Adaptive Lock smoothing. No HLS in the live path. Sub-second latency, works on iPad+Mac+Firefox+Safari, already proven this morning.
- Replay = the existing HLS + sidecar + `tools/sync_replay_assert.py` harness. Used for annotation verification, "rewind to 14:23:05" feature. NOT the live path.
- Two paths, each matched to its actual requirement.
- *Hidden assumption to challenge:* "live view doesn't need frame-accurate sync." Is that true? What if a bird is at the edge of a 200ms-skewed bbox?

**C11 — D2: Burn labels into the encoded video.**
- Pi draws bboxes onto YUV before encode, browser plays naked video, perfect sync by construction.
- *Hidden assumptions to challenge:* (a) Pi 5 has no H.264 hardware encoder — so encode is CPU-bound, possibly worse than now; (b) lose client-side toggle. Is there a cheap encode path I'm missing (e.g., using the camera's encoder, or only encoding the demo loop)?

**C12 — D3: Move HLS+segmenter to iMac. Pi does only sense+detect+SSE.**
- Right-size each piece to the right hardware. Pi load drops massively. iMac has hardware codec + 8 idle cores.
- *Hidden assumptions to challenge:* (a) iMac availability — what's the live-view experience when iMac sleeps or is off-LAN? (b) tunnel topology — does the Cloudflare tunnel terminate at Pi or iMac, and what does that imply for routing?

**C13 — D4: Foundation-only fix. HW H.264 decode + single shared decoder. Keep current architecture.**
- Add `h264_v4l2m2m`, share one PyAV demuxer. Predicted CPU drop 213% → ~80%. Current architecture starts working because foundation can support it.
- *Hidden assumption to challenge:* "the architecture is fine, only the foundation is straining." If `h264_v4l2m2m` works as advertised, is everything else really OK, or are there latent issues my audit missed?

**C14 — Step 1 / Step 2 phasing for tonight + tomorrow.**
- Tonight: minimum viable WebRTC + canvas + SSE, no smoothing, just labels on birds. ~30 min from go.
- Tomorrow: add Adaptive Lock smoothing kernel.
- Later this week: replay endpoint on iMac.
- *Hidden assumption to challenge:* "minimum viable will not paint us into a corner that costs more later." Is the Step 1 code throwaway, or does it have a clean upgrade path to Step 2?

---

## David's hypothesis (to be tested)

> "Why aren't we just going straight H.265 as soon as it comes in and utilizing this hardware marvel that we have for all things? Maybe as it comes in, we decode it into / or re-encode it into H.265, and then everything after that is H.265, we get a lower bitrate, we have less to work on, the quality is comparable."

**Spirit of the hypothesis:** Use the hardware accelerators end-to-end. Stop doing in software what the chip has dedicated silicon for. This is correct in principle.

**Specific implementation to test:**
- (a) Can we get H.265 from the camera directly?
- (b) Can we get H.265 from go2rtc, which can transcode? If so, at what CPU cost where?
- (c) Can we use hardware H.264 decode on the Pi (V4L2 M2M)?
- (d) Can the GPU/ISP do the resize-to-640×360 instead of software?

The agents will tell us which of (a)/(b)/(c)/(d) is actually achievable.

---

## Three parallel investigation tracks

Each agent gets a focused scope. They run independently. Findings synthesize at the end.

### Track A — Codec & Decoder Audit

**Question:** Is software H.264 decode necessary on the Pi? What hardware acceleration is available and unused? Is H.265 an end-to-end win?

**Scope:**
- `pipeline/frame_capture.py` — how PyAV is configured, whether hardware codec hints are set
- `pipeline/hls_segmenter.py` — is the segmenter actually decoding or truly passthrough mux?
- `pipeline/hls_recorder.py` — if this competes too, account for it
- `go2rtc.yaml` / go2rtc config on Pi — what's the camera output codec? what transcoding options exist?
- UniFi Protect camera capabilities — does it support H.265 output? (We have a G4 Pro or similar)
- Pi 5 V4L2 M2M codec availability: `v4l2-ctl --list-devices`, `/dev/video*`
- PyAV / libav documentation for `h264_v4l2m2m` / `hevc_v4l2m2m` decoder usage

**Outputs:**
1. Confirm or refute C1 (two software decoders)
2. Confirm or refute C2 (no hardware codec in use)
3. Confirm or refute C7 (Pi 5 has no HW H.265 encode)
4. Confirm or refute C8 (camera can output H.265 natively)
5. Confirm or refute C9 (HW H.264 decode is achievable and big)
6. Concrete code change list with file paths + line numbers
7. Estimated CPU savings per change (cite measurement source)

### Track B — Pipeline Architecture & Hidden Cost Audit

**Question:** Where in the pipeline is there accidental inefficiency? Unnecessary copies, conversions, allocations, GIL contention, redundant work?

**Scope:**
- `pipeline/frame_capture.py` — what shape does data move in (YUV→BGR conversions, resizes, copies)
- `pipeline/process_thread.py` — what runs per frame and what runs per detection
- `pipeline/classifier.py`, `pipeline/pi_classifier.py`, `pipeline/hailo_classifier.py` — which path is active, what does it cost
- `pipeline/tracker.py` — per-frame state cost
- `pipeline/snapshot_writer.py` — encode cost and frequency
- `pipeline/sse_events.py` — serialization cost per event
- `bird_pipeline_v3.py` — thread setup, queue sizes, sleep behavior
- All `time.sleep(...)` calls — are any of them masking design issues?
- All `Queue.put/get` calls — any unbounded queues? any backpressure missing?
- All numpy operations — any per-frame allocations that could be pooled?

**Outputs:**
1. Catalog of inefficiencies with file paths + line numbers
2. Rank by expected CPU impact (high/med/low) with rationale
3. Confirm or refute C5 (24%-tier threads are classifier+postprocess+snapshot)
4. Identify any unnecessary work happening every frame that could happen less often
5. Identify any obvious bugs (memory leaks, thread starvation, etc.)

### Track C — Architectural Alternatives Audit (challenges my conclusions AND my brainstorm)

**Question:** Are my proposed options (1/2/3) correctly framed? Are my brainstormed directions (D1-D4) defensible? Am I missing a better path? Are my claims about Option 1's cost (~150 lines) and benefit (halves decode) accurate?

**Scope:**
- The handoff doc: `docs/working/progress/2026-05-10-overlay-sync-handoff.md`
- The spec: `docs/working/specs/2026-05-10-pi-overlay-sync-bedrock-design.md`
- The plan: `docs/working/plans/2026-05-10-pi-overlay-sync-bedrock.md`
- This document itself, especially **claims C1-C14** including the brainstorm
- go2rtc documentation — does it support HLS output with the same PTS we need? At what cost?
- The actual code that would change for Option 1: `pipeline/frame_capture.py` + `pipeline/hls_segmenter.py` + `bird_pipeline_v3.py`
- The dashboard live-view code: `dashboard/pi_dash.html` (current HLS+canvas in `setupLiveView`, plus the WebRTC+DOM version recoverable from git history before commit `1503435`)

**Outputs:**
1. For each of my claims C1-C14: agree / disagree / requires evidence
2. For each direction D1-D4: estimate effort, identify hidden complexity, name failure modes I haven't named
3. **Specifically challenge D1's hidden assumption** that "live view doesn't need frame-accurate sync." Is sub-second skew acceptable for a backyard bird observatory, or will it make labels visibly wrong on flying/jumping birds?
4. For Option 1 vs D4 vs the morning's working WebRTC state: which is the smallest change that gets labels on birds tonight? Which is the smallest change that's also bedrock for the next 18 months?
5. Identify any **D5+** I haven't considered
6. Identify if my framing of the problem itself is wrong (e.g., maybe the right fix isn't on the Pi at all — maybe go2rtc box / iMac / different hardware)

---

## Parallel track: Cooling fix (independent of code)

Runs alongside the code audit. Independent because cooling affects every option equally.

**Current state:** GPIO-wired fan, standard pin. Not currently controlled — kernel doesn't know about it.

**Action:**
1. Identify the GPIO pin (David's memory or visual inspection)
2. Add `dtoverlay=gpio-fan,gpiopin=<N>,temp=60000` to `/boot/firmware/config.txt` so the kernel turns it on at 60°C
3. Reboot, verify thermals
4. Re-measure CPU envelope with fan working

**Expected outcome:** Sustained temp drops from 84-85°C to <70°C, throttle bits clear. This unlocks ~10-20% additional effective CPU on top of whatever architectural change we make.

This is a **5-minute fix** but it shouldn't change the architecture decision. We do it because it's right, not as a substitute for fixing the design.

---

## Decision criteria

Synthesis after all three tracks return. Decision driven by **measured deltas**, not opinion:

1. **If Track A confirms `h264_v4l2m2m` is available and easy to use in PyAV:** that change goes in first regardless of architectural option. Measure CPU drop. Re-decide.
2. **If camera supports native H.265:** plan a separate spec for switching go2rtc + pipeline to H.265 ingestion.
3. **If Option 1 saves ≥40% CPU and Track C finds no fatal flaw:** that's the architecture.
4. **If Track C identifies an Option 4+ that's cleaner:** consider it on equal footing.

Whatever direction we pick, **the new design must have a Q2 gate** (load test for 15 min, segmenter PTS within 2s of FrameCapture PTS, fail-fast) before any code goes into production. That gate becomes a committed script, runs on the Pi.

---

## What success looks like

- All three agents return findings within their scope
- Each of my 9 claims has been confirmed, refuted, or marked "needs more measurement"
- David's H.265 hypothesis is concretely answered: which path is real
- We have a ranked list of code changes with expected CPU impact
- We pick a direction with evidence, not vibes
- Fan starts cooling

## What failure looks like

- Agents return vague findings or just re-state my claims
- We pick a direction because it's the one I had in mind, not because it won on data
- We skip the Q2 load gate and ship another correctness-only plan
- We assume the throttle is the problem and don't address the underlying decode cost

---

## Files this audit references (for agents to read)

**Pipeline code (iMac path / Pi path):**
- `/Users/vives/bird-classifier-pi/pipeline/frame_capture.py` ↔ `/home/vives/bird-classifier/pipeline/frame_capture.py`
- `/Users/vives/bird-classifier-pi/pipeline/hls_segmenter.py` ↔ Pi
- `/Users/vives/bird-classifier-pi/pipeline/hls_recorder.py` ↔ Pi
- `/Users/vives/bird-classifier-pi/pipeline/process_thread.py` ↔ Pi
- `/Users/vives/bird-classifier-pi/pipeline/snapshot_writer.py` ↔ Pi
- `/Users/vives/bird-classifier-pi/pipeline/classifier.py`, `pi_classifier.py`, `hailo_classifier.py` ↔ Pi
- `/Users/vives/bird-classifier-pi/pipeline/sse_events.py` ↔ Pi
- `/Users/vives/bird-classifier-pi/bird_pipeline_v3.py` ↔ Pi
- `/Users/vives/bird-classifier-pi/dashboard/api.py` ↔ Pi (the HLS proxy lives here)

**Pi runtime state (SSH access required):**
- `vives@pi5.local:~/bird-classifier/` — live deployed code
- `vives@pi5.local:/boot/firmware/config.txt` — boot config (dtoverlays)
- `vives@pi5.local:~/.config/go2rtc/go2rtc.yaml` — go2rtc config
- `vives@pi5.local:~/.config/systemd/user/bird-pipeline.service` — service env

**Specs / handoffs to challenge:**
- `/Users/vives/bird-classifier-pi/docs/working/specs/2026-05-10-pi-overlay-sync-bedrock-design.md`
- `/Users/vives/bird-classifier-pi/docs/working/plans/2026-05-10-pi-overlay-sync-bedrock.md`
- `/Users/vives/bird-classifier-pi/docs/working/progress/2026-05-10-overlay-sync-handoff.md`
- This file

---

## Execution order

1. **Now (this turn):** Save this plan. Save Codex paste-ready prompts to a sidecar file. Show David.
2. **Wait for David's go-ahead.**
3. **On go:** Dispatch my three parallel agents (Track A, B, C). David pastes the Codex versions on his MBP in parallel.
4. **Cooling parallel track:** David and I sort out the fan dtoverlay while agents run.
5. **Findings synthesis:** I aggregate all six investigations (3 mine + 3 Codex), reconcile disagreements, present a ranked change list with expected impact and Q2 gate.
6. **David picks direction.**
7. **Write the implementation spec** for the chosen direction, with a **load gate (Q2) as a required acceptance criterion**.
8. **Implement with subagent-driven development**, two-stage review, no patch culture.
