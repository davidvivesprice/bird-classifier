# Bird Observatory — Roadmap & Scoreboard

> **This is the spine. It is a thin ledger, not a book.** If it ever starts
> becoming a beast, stop and cut it back. The book attempt taught us that a
> document meant to *create* clarity can become a project that *consumes* it.
> Each chapter below has one binary "chip" — green or not. We work **one
> chapter at a time** and keep everything else quiet.

_Last updated: 2026-06-15 · Owner: David · Active surface: Raspberry Pi 5_

---

## TL;DR

We build the bird observatory in three chapters, in order. Chapter 1 is the
keystone and needs nothing else to stand up.

| # | Chapter | One-line chip (is it green?) | Status |
|---|---------|------------------------------|--------|
| **1** | **Live identification** | A bird arrives; a correct, well-timed label sticks to it through every hop with no flicker, handles 2+ birds, leaves shortly after — **proven by automated offset measurement, not by eye.** | 🔴 not landed |
| **2** | **Clean, accurate data** | Pull N recent detections: each is genuinely the claimed bird (or honestly "unknown"), with a high-quality crop, tight bounding box, and correct metadata. | 🔴 not landed |
| **3** | **Presentation** | The data is delightful to look at. (Raw numbers are fine until 1 & 2 are green.) | ⚪ deferred |

**✅ Done 2026-06-15 — fake feed is off the Pi.** The demo loops as an always-on
compose service on the NAS (`/volume2/docker/bird-demo`, mediamtx+ffmpeg),
served at `rtsp://192.168.4.243:8554/feeder-main`. The Pi consumes it exactly
like the real camera; the on-Pi loop is retired. Dashboard verified showing
the NAS demo with live labels.

**The next concrete move:** build the **empirical offset rig** (Chapter 1,
step 2) and **strip the Pi to live-ID only** (step 3) so we measure the live
path cleanly. See the refined sequence at the bottom and "What we learned"
below.

---

## How we got stuck, and the cure

We didn't lack progress — we shipped a lot. We lacked **closure**. Every
thread (overlay sync, thermals, the zombie, the enclosure, the docs, the book)
stayed open at once, so nothing ever reached "done, stop looking." The cure is
structural, not more effort:

1. **One chapter at a time.** Binary chips. Everything not in the current
   chapter goes quiet.
2. **The Mac is a frozen reference.** The 2017 iMac observatory is the
   known-good example. We do **not** work on it right now — we read from it.
   The Pi is the single active surface.
3. **Measure, don't describe.** (See below — this is the big one.)
4. **Thin ledger, not a book.** This file stays boring.
5. **Don't reinvent the wheel.** This problem is largely solved elsewhere; we
   apply prior art rather than invent. (See "Prior art to mine.")

### The measurement principle (this is what broke us before)

The "show David → David says it's a little behind / a little ahead → adjust →
repeat" loop cost us days and never converged. **The human eye is a reliable
binary judge ("yes that's right" / "no it's off") but a terrible analog
instrument** for quantifying offset. So we take the eye out of the measurement:

- Loop the demo video with a **burned-in / synced timecode** so the Pi knows
  exactly *when* it should see *what*.
- We already annotated that loop **frame-by-frame** (which bird enters/leaves
  when). Combined with the timecode, the system can report the **exact overlay
  offset in milliseconds** automatically.
- A harness already exists for this: `tools/sync_replay_assert.py` +
  `tools/annotation_parser.py` + the annotation fixture. The offset becomes a
  number on every change, not a conversation.

**Rule: no overlay-timing work gets judged by describing it to each other.**
Set up the empirical rig first; then every change is measured.

---

## Chapter 1 — Live identification (the keystone)

The most important chapter and the most independent. Strip the Pi to one job:
**receive a stream → detect → track → classify → put a label on the bird.**

It splits cleanly into two sub-chips. **1a can go fully green even while the
model is still bad** — that's why this chapter stands alone.

### 1a — Overlay/tracking fidelity (judge by eye + harness)
The label is glued and alive:
- Sits on the bird, moves with it, fades in as it arrives, fades out shortly
  after it leaves.
- Smooth, denoised motion (the tracking work we already did).
- **No "lost it / found it" identity churn.** A perched bird keeps one stable
  track.
- Handles **multiple birds in frame at once.**
- **No perceptible timing lag**, verified by the offset harness (target band
  TBD, e.g. ±1 frame).

This does **not** depend on the model being accurate. The label being
pixel-glued to the bird is deliberately kept as the **measurement instrument**
— drift is instantly visible. (Prettier presentations — a name card up in the
empty space above the feeder, the zoomed bird-photo card we already built —
are a Chapter 3 choice. Not now.)

### 1b — Decision quality (behavior, not accuracy)
- Uses its time well: accumulates votes and commits to the best species call
  it's going to get, then holds it.
- Stable per track; doesn't thrash its guess.
- *Whether the guess is **correct** is Chapter 2.* Here we only care that the
  **timing/commit behavior** is right.

### Enabler ✅ DONE (2026-06-15): fake feed is off the Pi
The demo loops as an always-on compose service on the NAS
(`/volume2/docker/bird-demo`, mediamtx + bundled ffmpeg, watchtower-excluded),
served at `rtsp://192.168.4.243:8554/feeder-main`. The Pi consumes it exactly
like the real camera (FrameCapture + segmenter both on the NAS 640×360 feed,
verified — zero connections to the real camera in demo mode). On-Pi
`bird-demo-loop` retired; `/api/demo-mode` now just repoints the pipeline at
the NAS feed. Self-inflicted produce+consume+infer load is gone.

### What we learned standing it up (2026-06-15)
- **Demo mode runs hotter than live (139%/82°C vs ~72%/65°C)** — expected: the
  demo loop is **bird-dense**, so YOLO+AIY classify constantly. It's a good
  worst-case stress test, not a regression. BUT the **HLS segmenter is also
  running and is pure overhead for live-ID** — pausing it is the first item in
  the strip-down (step 3) and the cleanest load win.
- **The WebRTC live path can't self-report which frame is on screen** — so we
  can't measure overlay offset from the player's timeline. The fix (David's
  own idea): **burn a timecode into the demo video**; a probe reads the
  on-screen timecode + the label position from pixels and compares to the
  annotations → offset in ms, transport-agnostic. This is how the empirical
  rig (step 2) gets built, and it also informs the 1a fork (simple WebRTC path
  vs. spatial-subtitle): we measure first, escalate only if we must.

### Diagnostic: is it load, or is it code?
Open question David raised: when timing is off, is the Pi overloaded or is
there a code/sync bug? **Answer it by removing variables, cheapest first:**
1. Move the feed off the Pi (above). Re-measure. If timing comes good → it was
   self-inflicted load. Chapter likely closes.
2. If still off → run the *same pipeline* on the **M4 Mac** (massively
   overpowered, no Coral). Timing perfect there but bad on Pi → it's Pi load.
   Bad on both → it's a **code/sync bug**, hunt that.
3. Only then consider giving the Pi a Coral. (Note: Coral's libedgetpu aborts
   on the iMac after power events — it's a known-flaky variable; don't add it
   until the question actually demands it.)

### Explicitly out of scope for Chapter 1 (paused, not abandoned)
- **Snapshots / high-res capture on the Pi** — paused to shed load. Returns in
  Chapter 2.
- **Mobile / iOS overlay** — important, **stays on the roadmap**, but does
  **not** need to land for us to know our overlays work. Desktop is enough to
  judge 1a. (Historically the gnarly corner: Cloudflare buffers SSE; the WS
  mirror is the current path.)
- **Presentation** — Chapter 3.

### A fork we'll hit (decide later, don't solve now)
"It knows what bird it is *before* it comes in" implies a small **built-in
display delay** — you can't label what you haven't seen and classified yet.
Truly-live (label catches up to the bird) vs. slightly-delayed (label
pre-formed as the bird arrives) is a real trade. Broadcast solves it with
delay (see prior art). We choose when we get there.

---

## Chapter 2 — Clean, accurate data

Accuracy is the foundation of every inference we'll ever build. Bad data = fake
data. We've done much of this thinking on the Mac already; here it's about the
Pi producing trustworthy records.

- **Fewer false positives.** The model should know better when it's a given
  bird and when it doesn't know.
- **Base-model decision:** is there a commercially-viable model we can use, or
  do we need to train our own dataset to reliably name the bird in real time?
- **The integrity chain:** isolate high-quality shots, attach correct
  metadata, render tight/correct bounding boxes. Triple-check each link.

Note: Chapters 1 and 2 share the **same detection/classification/crop core**,
judged by two different consumers (the live viewer vs. the database). Fixing
the core correctness pays both — not duplicated work.

**Chip:** pull N recent detections; each is genuinely the claimed species (or
honestly "unknown"), crop is high-quality, bbox is tight, metadata is correct.

---

## Chapter 3 — Presentation (deferred)

Make the data delightful — the bird cards, the stories, first-arrivals, peak
hours. Real, but **raw numbers are good enough until 1 & 2 are green.** Don't
let presentation into the room earlier; it blurs the signals we're measuring.

**Chip:** TBD when we get here.

---

## Prior art to mine (we are not on the edge of innovation)

David's instinct is right: people have solved near-live tracked overlays. We
should borrow, not invent. Strongest parallels:

- **Broadcast sports telestration / live player tags** (the yellow first-down
  line; soccer name tags that follow players). This is *our exact problem*,
  solved for decades. The universal trick: **"live" TV is actually delayed a
  few seconds**, and frame-accurate tracking data is synced to the *delayed*
  video. Validates the small-display-delay approach.
- **AR object anchoring** (ARKit/ARCore): labels stuck to moving real-world
  objects via tracking + motion prediction.
- **Open-source NVR overlays** (Frigate, Viseron, Blue Iris): live
  bounding-box overlays on RTSP camera feeds — the closest *domain* parallel.
  Worth studying how Frigate sequences detection → overlay timing.
- **Live captioning / subtitle cue model**: cues carry timestamps and play
  against a media clock. (Captioning tolerates latency, so it's a partial
  match — but the *cue-scheduling against a media clock* model is exactly
  right, and it's the same idea as our PTS-as-the-only-clock rule.)

**Synthesis:** the wheel = **tracking-with-prediction + a deliberate small
display delay so graphics are computed and synced before they're shown.** We
are *applying* this, not inventing it. (A deeper survey is a good discrete task
when we open Chapter 1.)

---

## Resources available

- **M4 Mac** — powerful; can host the RTSP demo loop, be an overpowered
  testing ground for the load-vs-code experiment, or run a reference pipeline.
- **NAS (VivesNasty, Synology, 192.168.4.243)** — always-on, Docker host; the
  natural home for the RTSP demo loop. Also the UPS/NUT primary.
- **Existing harness:** `tools/sync_replay_assert.py`, `tools/annotation_parser.py`,
  the frame-by-frame demo annotations.
- **Existing overlay stack:** WebRTC + DOM labels with CSS smoothing
  (`dashboard/pi_dash.html`), HLS segmenter + PTS sidecar
  (`pipeline/hls_segmenter.py`) for replay/measurement.
- **Self-heal + power resilience:** `deploy/` (watchdog, service-canary,
  pi-watch), NUT graceful shutdown — all landed; keeps the Pi alive while we
  work the chapters.

---

## Section for the coding AI

Read this before touching anything; it encodes hard-won constraints.

**Where the code lives**
- Pi repo (active): `/Users/vives/bird-classifier-pi/`, branch `pi-main` →
  `imac-origin/pi-main`. Edit here, rsync to `vives@pi5.local:/home/vives/bird-classifier/`.
- iMac repo (frozen reference, do **not** develop): `/Users/vives/bird-classifier/`,
  branch `main`. Read for patterns only.
- Docs reference book: `~/docs/bird-observatory-pi/` (a separate Claude owns
  doc-sync; coordinate via `docs/working/progress/cross-claude-comms.md`).

**Load-bearing constraints (violating these is how we lost weeks)**
- **PTS is the only clock for sync decisions.** Wall-clock is fine for log
  lines and filenames, never for deciding which video frame a label belongs
  on. (`feedback_port_the_clock_not_just_the_math`.)
- **Measure overlay timing empirically.** Never iterate via "does this look
  right to you." Use the timecode + annotation + `sync_replay_assert` rig and
  report offset in ms.
- **The demo feed runs OFF the Pi** (NAS or M4 Mac) as RTSP. Never make the Pi
  both produce and consume the test stream.
- **One chapter at a time.** If you're touching snapshots/presentation/mobile
  while Chapter 1 is open, stop.
- **Pi 5 has no hardware H.264 decoder** (HEVC decode only, no encoder of
  either) — software-decoding 1080p is expensive; prefer the camera substream.
  (`feedback_pi5_rtl9210_boot`, the 2026-05-11 thermal triage.)

**Prior, relevant design work to read (don't re-derive)**
- `docs/working/specs/2026-05-11-spatial-subtitle-overlay-architecture.md` —
  the delayed-display "labels as spatial subtitles over a media clock"
  architecture (the broadcast-style approach). The most complete answer to
  Chapter 1's timing fork.
- `docs/working/progress/2026-05-11-overnight-result.md` — the WebRTC+DOM
  restoration + what's deferred.
- `~/docs/bird-observatory-pi/10-overlay-sync.md` — the full overlay history
  with the LIVE/DEFERRED/PAUSED/DEAD avenues table (so you don't re-chase
  dead paths, e.g. HW H.264 decode on Pi 5).

**Chapter 1 sequence (measure-first)**
1. ✅ Stand up the RTSP demo loop on the NAS; point the Pi at it. *(2026-06-15)*
2. ⏳ **NEXT — empirical offset rig:** re-encode the demo with a **burned-in
   timecode/frame-number**, redeploy to the NAS loop, then a probe reads the
   on-screen timecode + label position from pixels and compares to the
   annotations → offset in ms. Transport-agnostic (works for WebRTC+DOM today).
3. ⏳ **Strip the Pi to live-ID only:** pause the HLS segmenter + snapshots in
   demo mode (sheds the unnecessary load identified above). Decode → motion →
   YOLO → track → classify → label, nothing else.
4. Measure. Decide load-vs-code per the diagnostic ladder above (M4 as the
   overpowered control).
5. Fix to green on 1a, then 1b. Each change re-measured automatically.
