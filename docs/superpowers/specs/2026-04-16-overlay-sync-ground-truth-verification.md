# Overlay Sync — Ground-Truth Verification Design

**Date:** 2026-04-16
**Status:** ⚠️ **SUPERSEDED 2026-04-17** — the architecture pivoted away from
ffmpeg PDT-based sync to a Python-stamped sidecar manifest, sidestepping the
entire verification cascade below. See "What changed and why" right below this
banner before reading further.

---

## ⚠️ READ THIS FIRST — what changed since this doc was written

This spec described a verification phase for an approach that the team
**abandoned the next day** (2026-04-17). Anyone reading this in 2026-04-25+
without this banner has gotten confused (literally happened to me). The
header below preserves the original spec for historical context, but the
critical updates are:

1. **The `OVERLAY_LEAD_COMPENSATION_MS = 1000` band-aid mentioned in §1
   was REMOVED.** Verify with `grep OVERLAY_LEAD_COMPENSATION
   ~/bird-classifier/dashboard/live.html` — returns nothing.

2. **PDT (`hls.playingDate`) is no longer used for overlay sync.** The
   current architecture uses `pipeline/hls_recorder.py::_manifest_loop`
   to write `segments.json` with `completed_ms = int(st.st_mtime * 1000)`
   per .ts segment. The browser fetches that sidecar and derives the
   displayed frame's wall-clock from `completed_ms - duration*1000 +
   offset`. Both this `completed_ms` AND the SSE event `wall_time_ms`
   are stamped by Python `time.time()` on the iMac at corresponding
   pipeline stages, so they cancel for relative alignment regardless of
   absolute clock truth.

3. **Gate 1a's iMac-NTP-+180ms finding (still true) is NO LONGER in the
   dependency chain.** The sidecar approach is NTP-independent by design.
   The whole "no drift ever" requirement is now satisfied by sharing one
   Python clock source for both stamps, not by chasing camera↔NTP truth.

4. **Gates 0, 1b, 2, 3, 4, 4b — all "pending" below — were never run.**
   They were obviated by the pivot. Status fields in §9 are stale.

**For the current overlay sync architecture, read in this order:**
- `2026-04-17-smooth-label-overlay-design.md` (the pivot to sidecar +
  Catmull-Rom smoothing)
- `~/docs/bird-observatory/31-label-motion-adaptive-lock.md` (April 18:
  Catmull-Rom replaced by Adaptive Lock — Gaussian kernels + velocity
  blend, the version currently shipping on `/live`)
- `~/bird-classifier/docs/superpowers/specs/2026-04-25-imac-live-classify-as-built.md` §2
  (the two clocks reconciliation, code-level citations)

The original spec is preserved below for the record (and because the
gate-by-gate verification methodology was sound; if we ever pivot BACK
to PDT-based sync, this is how to verify it).

---

**Successor to:** `2026-04-15-delayed-playback-overlay-design.md`

## Execution checklist

Follow in order. Snapshot preconditions before each gate; snapshot postconditions after. If any postcondition differs from its precondition, flag and investigate before continuing.

- [ ] Gate 1a — iMac + CloudKey NTP (direct measurement)
- [ ] Gate 0 — RTCP Sender Report capture (presence + parse only; offset measurement deferred to 1b)
- [ ] Gate 1b — camera↔iMac offset computed from Gate 0 packets
- [ ] Gate 2 Method A — read ffmpeg `hlsenc.c` PDT emission path
- [ ] Gate 2 Method B — 60s short-window stability of PDT against physical reference
- [ ] Gate 2 Method C — 30min long-window drift of PDT against physical reference
- [ ] Gate 3 — go2rtc RTCP exposure (in parallel with Gate 4)
- [ ] Gate 4 Part 1 — PyAV install + decode smoke test (in parallel with Gate 3)
- [ ] Gate 4 Part 2 — source read of `rtpdec.c` / `rtsp.c` to certify `start_time_realtime` provenance (MANDATORY, even if Part 1 passes)
- [ ] Gate 4b — sidecar ffmpeg fallback (only if Gate 4 fails AND Gate 2 passes)
- [ ] Recommendation written in Section 10

## 1. Problem statement

The delayed-playback `/live` page draws bounding boxes roughly **one second ahead** of the bird visible in the video. The SSE-event-to-`playingDate` match distance measures at ~80 ms, so the timestamp-matching math is correct. The offset lives in the stamping itself: what the pipeline calls "wall_time_ms" and what HLS calls `#EXT-X-PROGRAM-DATE-TIME` do not refer to the same moment in physical time.

A constant client-side compensation has been added as a stopgap (`OVERLAY_LEAD_COMPENSATION_MS` in `dashboard/live.html`, default 1000). That is explicitly a band-aid. This spec defines the verification work required before we replace the band-aid with a drift-proof fix.

## 2. Success criterion for this verification phase

A written record (this document, updated in place) in which each gate below has exactly one of the following statuses, with cited evidence:

- `[VERIFIED-DOCS]` — confirmed by reading authoritative documentation. **Evidence must include:** URL, commit SHA or version tag, and a directly quoted sentence or clause that supports the claim.
- `[VERIFIED-CODE]` — confirmed by reading the actual source we execute on this machine. **Evidence must include:** absolute file path, line range, commit SHA or version, and the specific code lines that support the claim.
- `[VERIFIED-TEST]` — confirmed by a repeatable measurement on this system. **Evidence must include:** the command run, raw output (or a pointer to a saved log), the measurement statistic, and the pass criterion it met.
- `[INCONCLUSIVE]` — we do not yet know; the doc states precisely what would make it conclusive and why we couldn't get there.

No gate concludes "probably yes." A tag without the required provenance block is treated as INCONCLUSIVE. If we cannot verify, we say so and stop.

## 3. Non-goals of this phase

- No edits to `pipeline/frame_capture.py`, `pipeline/hls_recorder.py`, `dashboard/live.html`, or `dashboard/hls-test.html`.
- No restart of `bird_pipeline_v3`, `hls_recorder`, or any ffmpeg subprocess.
- No ffmpeg flag changes on running processes.
- No implementation plan — that comes after this doc is reviewed and approved, via the `writing-plans` skill.

**Enforcement — precondition and postcondition snapshots:** rules are honor-system without verification. Before and after every gate, we snapshot:
- `launchctl list | grep -E 'bird|pipeline|cloudflared'` — pipeline process state.
- `curl -s http://192.168.4.200:1984/api/streams` — go2rtc session list (structural diff: same stream names, not identical JSON).
- `test -d ~/bird-snapshots/hls/feeder/ && ls ~/bird-snapshots/hls/feeder/*.m3u8` — HLS manifest present. **Not** the segment list: segments rotate naturally every 5s and will always differ between snapshots. We confirm the directory + playlist file exist, not that its contents are byte-identical.
- `pgrep -f hls_recorder && pgrep -f bird_pipeline_v3` — PIDs unchanged (compared numerically, not via concatenation).

If any of these change between pre and post in a way not explained by natural rotation, the gate result is flagged and we investigate before proceeding. Gate 0 in particular is called out: opening a second RTSP session to the camera can evict go2rtc's session depending on camera session limits, so its precondition snapshot is load-bearing.

## 4. Gates

### Gate 0 — RTCP Sender Report presence and quality

**Question:** Do the UniFi G3 Dome cameras actually emit RTCP Sender Reports (SR) on the RTSP streams we consume, at a useful interval, with an NTP field sourced from an NTP-disciplined clock?

**Why it matters:** Gates 2 and 3 both presume the RTP↔NTP mapping carried in RTCP SR is available and trustworthy. If the cameras don't emit SR, or emit them stale, or emit them with an NTP field that's just `time.time()` of the camera without NTP sync, then the entire "RTP-anchored truth" approach is a fantasy and we must fall back to a different anchor (e.g., go2rtc ingest boundary with explicit offset measurement).

**Method:**
- `ffmpeg -rtsp_transport tcp -i <feeder_rtsp_url> -loglevel trace -t 30 -f null -` captured to a file; grep for `RTCP` / `sender report` / `NTP timestamp`. Count SR packets; record inter-arrival interval; record the NTP field of each.
- If ffmpeg's log is insufficient, `tcpdump -i any -w rtcp.pcap 'port 554 or (udp and portrange 5000-6000)'` for 30s then open in Wireshark to inspect SR packets directly.
- Cross-check camera NTP config via Protect API (`GET /proxy/protect/api/cameras/{id}`), looking for NTP server fields.

**Pass criterion:** at least one SR packet captured per 10s of stream; NTP field present and parses as a valid NTP timestamp; camera has an NTP server configured. Quantitative offset measurement is deferred to Gate 1b (below) to avoid duplicating threshold definitions.

**Fail criterion:** no SR packets in 30s, or NTP field fails to parse, or camera has no NTP server configured.

**Caveat:** running ffmpeg or tcpdump against the camera opens a second RTSP session. Check the precondition snapshot (Section 3) before and after; if go2rtc's feeder session is evicted, fall back to tcpdump against the go2rtc→ffmpeg stream inside the iMac rather than re-contacting the camera.

**Status:** _[pending]_

---

### Gate 1 — Clock ground truth

**Question:** Do the iMac, CloudKey, and UniFi cameras agree on what time it is, within a usable tolerance?

**Why it matters:** Every downstream timestamp is expressed relative to some clock. If the underlying clocks disagree by more than a few tens of milliseconds, no amount of timestamp plumbing produces a pinned-to-truth overlay. This gate sets the noise floor for every other gate.

Gate 1 is split into two sub-gates because the camera leg depends on Gate 0's output.

#### Gate 1a — iMac + CloudKey direct measurement

**Method:**
- `ntpq -p` on the iMac — record stratum, source, offset, jitter.
- CloudKey NTP status via its admin page or `ssh`.

**Pass criterion:** iMac and CloudKey synchronized to the same source with measured offset < 50 ms (via `ntpq`).

**Fail criterion:** either device unsynchronized, or measured offset > 100 ms.

**Status:** _[pending — first to run]_

#### Gate 1b — Camera↔iMac offset via RTCP SR

**Known limitation:** UniFi Protect does not expose a measured offset for cameras — only "configured NTP server." Direct sub-second offset measurement for cameras is not available through the API.

**Method:** compute camera offset from Gate 0's captured RTCP packets using kernel-pcap-timestamped reception:
- Use `tcpdump -tt -i any -w rtcp.pcap 'tcp port 554'` during Gate 0 capture. The `-tt` flag records packet arrival in Unix seconds from the kernel's NTP-disciplined pcap clock (same clock basis as `ntpq` measurements, _not_ ffmpeg's internal monotonic).
- Parse captured pcap with `tshark -r rtcp.pcap -Y rtcp.sr -T fields -e frame.time_epoch -e rtcp.senderssrc -e rtcp.timestamp.ntp` (or equivalent).
- For each SR packet: `offset_i = SR.NTP_seconds − frame.time_epoch`. Record N samples across 60 s.
- Report: `median(offset_i)`, `p95(|offset_i − median|)`.

**Pass criterion:** camera NTP server is configured (per UniFi Protect API), AND `median(offset_i)` is within 100 ms of zero, AND `p95` dispersion is under 50 ms (indicating stable, not jittering).

**Fail criterion:** camera NTP unconfigured, median > 100 ms, or dispersion > 50 ms (indicating an unstable camera clock that would drift inside a 30-min window).

**Status:** _[pending — runs after Gate 0]_

---

### Gate 2 — ffmpeg PDT semantics

**Question:** Does ffmpeg's `-hls_flags program_date_time` anchor the `#EXT-X-PROGRAM-DATE-TIME` value to the source RTP timestamp (mapped via RTCP Sender Report to camera NTP), or does it stamp system wall-clock time at the moment ffmpeg writes a segment to disk?

**Why it matters:** This single question decides the scope of the eventual fix.
- If PDT is RTP-anchored, the HLS side is already honest, and only `frame_capture.py` needs to change. Small surgery.
- If PDT is ingest-wall-clock-anchored, both sides are stamping dishonestly, and the fix must replace at least one of the two ffmpeg subprocesses.

**Important framing:** Method B below cannot, on its own, distinguish "PDT is RTP-anchored" from "PDT has a stable ingest offset." The external clock-face reading goes through (display latency → camera sensor → camera encoder buffering → PDT stamp), all smeared into one observation. Method A (reading ffmpeg source) is the authoritative answer; Method B is a cross-check that PDT is at least _stable_ against physical time.

**Method A — read the source (authoritative, load-bearing):**
- Locate ffmpeg source for this Homebrew install (`brew --prefix ffmpeg` + Formula `head` URL; or `ffmpeg -version` to get the tag and fetch from git mirror).
- In `libavformat/hlsenc.c`, find the code that emits the `EXT-X-PROGRAM-DATE-TIME` line. Record file, line range, commit SHA.
- Trace backward to identify the timestamp source. The answer must be one of:
  - **RTP-anchored:** PDT derives from demuxer `AVFormatContext.start_time_realtime` + per-packet `pts`/`dts` mapped through the RTSP demuxer's RTCP-SR handler.
  - **Ingest-anchored:** PDT derives from `av_gettime_relative()` / `av_gettime()` / `time(NULL)` called at segment-write time.
- Cite the exact function(s) and branches. This is the `[VERIFIED-CODE]` claim that decides scope.
- **Scope boundary:** Method A reads _only_ `hlsenc.c` and what it directly references. The upstream question of whether `start_time_realtime` itself is populated from RTCP SR and refreshed over time is verified separately in Gate 4 Part 2 (`rtpdec.c` / `rtsp.c` read). Do not duplicate that read here. If Method A says "PDT reaches for `start_time_realtime`," Gate 4 Part 2 is what certifies that source is actually SR-anchored.

**Method B — short-window stability cross-check (NOT a truth test):**
- Display a millisecond-resolution clock face on a second monitor in the camera's field of view.
- Record for 60 s.
- Extract 5 HLS frames spread across 3 different segments. For each, read the clock face and compute `delta = PDT_for_frame − clock_face_reading`, where `PDT_for_frame = segment_PDT + (frame_offset_within_segment / frame_rate)`.
- Record the 5 deltas.

**Method B pass criterion:** deltas are within 200 ms of each other (stability check). The _absolute value_ of the delta is not interpreted — only its variance.

**Method B fail criterion:** deltas vary by more than 500 ms across frames, or correlate with frame-offset-within-segment (suggesting segment-boundary stamping artifacts).

**Method B PASS does NOT conclude Gate 2 — Method C is still required.** Short-window stability cannot distinguish "PDT is truly RTP-anchored" from "PDT is latched once and extrapolating." Proceeding without Method C would fail the "no drift ever" directive.

**Method C — long-window drift test (REQUIRED for "no drift ever" directive):**
A source read (Method A) can only show which timestamp ffmpeg *reaches for*. It cannot distinguish:
- "PDT refreshes from every RTCP SR" (truly RTP-anchored, no drift) from
- "PDT latches the initial SR and advances purely by PTS extrapolation" (stable over seconds, drifts over hours as camera clock drifts against iMac clock).

Method B's 60 s window cannot distinguish these either. Therefore:
- Record a sample segment's PDT-vs-clock-face delta at `T=0`, `T=10 min`, `T=20 min`, `T=30 min` (4 samples across 30 minutes of uninterrupted stream).
- Compute the drift slope `ms_of_delta_change_per_minute`.

**Method C pass criterion:** `|slope|` × 60 < Gate 1's camera offset tolerance (100 ms/hour equivalent). Concretely: total drift across 30 minutes is < 50 ms.

**Method C fail criterion:** drift slope exceeds that bound — means PDT is not being refreshed from continuing SRs and the "unshakable" claim fails at long time scales.

**Gate 2 overall pass:** Method A identifies PDT as RTP-anchored AND Method B shows stable short-window deltas AND Method C shows bounded long-window drift.
**Gate 2 overall fail:** Method A identifies PDT as ingest-anchored, OR Method B shows unstable deltas, OR Method C shows unbounded drift (even if Methods A and B pass).
**Gate 2 INCONCLUSIVE:** Method A ambiguous (e.g., path-dependent timestamp source), or Method C cannot be run (camera stream interrupts during the 30-min window) — document what would disambiguate and stop.

**Status:** _[pending]_

---

### Gate 3 — go2rtc RTCP exposure

**Question:** Does go2rtc expose the RTP-to-NTP mapping from RTCP Sender Reports through its HTTP API, such that a Python client could ask "what NTP time corresponds to the current RTP timestamp on the feeder stream"?

**Why it matters:** If yes, we can compute truth once in Python and distribute it, without having to parse RTCP ourselves. This is especially relevant if Gate 2 fails and we need option (b) or (c) from the decision tree.

**Method A — GET-only endpoint probe (no POST/PUT/DELETE anywhere):**
- `curl -s http://192.168.4.200:1984/api/streams?src=feeder`
- `curl -s http://192.168.4.200:1984/api/streams` (list form)
- `curl -s http://192.168.4.200:1984/api`
- `curl -s http://192.168.4.200:1984/api/ffmpeg`
- `curl -s http://192.168.4.200:1984/api/info`
- `curl -s http://192.168.4.200:1984/api/config`
- `curl -s http://192.168.4.200:1984/api/frame.json?src=feeder`
Record the JSON response of each. We are looking for fields named any of: `ntp`, `rtcp`, `sr`, `start_time`, `timestamp`, `pts`, `epoch`, `wallclock`. This list is "known endpoints as of the currently deployed version." If Method B's source read reveals an endpoint not on this list that exposes timing data, re-hit with GET only and record. No other endpoints are probed in this phase.

**Method B — source reading:**
- Clone or browse `AlexxIT/go2rtc` at the tag matching the deployed version (`curl http://192.168.4.200:1984/api/info` to identify version). Record commit SHA.
- Grep for RTCP SR handling (`SenderReport`, `NTP`, `senderReport`, `RTCP_SR`). Identify whether the NTP mapping is retained in memory and whether any HTTP handler exposes it.

**Pass criterion:** at least one API surface returns a structure from which RTP→NTP can be derived without patching go2rtc.

**Fail criterion:** go2rtc parses RTCP but does not expose it via HTTP; patching or direct RTCP parsing in our code would be required.

**Status:** _[pending]_

---

### Gate 4 — PyAV feasibility

**Question:** Can PyAV on this iMac decode the feeder substream RTSP URL and produce per-frame PTS values that we can map to wall-clock time?

**Why it matters:** The small-surgery path (rewriting `frame_capture.py`) depends on PyAV. If it doesn't work here, we either compile it from source, find a different library, or fall back to a sidecar `ffmpeg -progress` approach.

**Method Part 1 — PyAV install and decode smoke test:**
- `pip show av` in the pipeline's active venv. If present, proceed. If not, install in a scratch venv to avoid perturbing production.
- Write a ~30-line script that opens the substream RTSP URL via PyAV, decodes 100 video frames, and for each prints: `frame.pts`, `frame.time_base`, `stream.start_time`, `getattr(stream, 'start_time_realtime', '<missing>')`, `container.start_time`.
- Defensive: use `getattr(..., default)` for `start_time_realtime` so a missing attribute produces a named FAIL ("attribute not bound in PyAV vX.Y") rather than an AttributeError.

**Method Part 2 — source read to verify `start_time_realtime` semantics (load-bearing, NOT optional):**

The claim "`stream.start_time_realtime` holds the RTCP-SR-anchored NTP epoch" is itself an unverified assumption until we read ffmpeg source. On some ffmpeg versions this field is initialized from `time(NULL)` at connection time and only later refreshed from SR packets — or not refreshed at all. If that's the case, a Gate 4 PASS plus Gate 2 PASS could still ship a drifting pipeline.

- Read `libavformat/rtpdec.c` and `libavformat/rtsp.c` in the ffmpeg source matching the deployed version. Record commit SHA.
- Find where `AVFormatContext.start_time_realtime` is assigned. Identify the source: is it the NTP field of the first RTCP SR packet, or `av_gettime()`/`time(NULL)` at connection open?
- Also identify whether the field is **refreshed** on subsequent SR arrivals, or latched once and left alone.
- Record the answer as a `[VERIFIED-CODE]` claim with path, line range, commit SHA, and quoted lines.

**Pass criterion (all must hold):**
1. All 100 frames decode without errors.
2. `frame.pts` values are strictly monotonically non-decreasing.
3. `frame.time_base` is consistent across frames (expected 1/90000 for RTP video, but we will record what it actually is, not assume).
4. `stream.start_time_realtime` is bound in the PyAV object AND populated with a non-None value.
5. **Source read (Method Part 2) confirms `start_time_realtime` is assigned from RTCP SR NTP on this ffmpeg version.** Without this confirmation, pass criterion 4 is meaningless.

**Explicit non-criterion:** we do NOT require "PTS agrees with time.time() within 1s." That comparison would reward reproducing the drift we're trying to eliminate. Feasibility of the library is decoupled from correctness of the derived wall-clock.

**Fail criterion:** install fails, decode fails, PTS non-monotonic, PTS resets across segments, `start_time_realtime` attribute unbound in this PyAV version, `start_time_realtime` value is None, OR source read reveals the field is populated from ingest wall-clock rather than SR NTP.

**Partial pass → INCONCLUSIVE:** if `start_time_realtime` is SR-derived but only latched at connection (never refreshed), the field is truth at T=0 but drifts thereafter. This matches the long-window-drift failure mode Gate 2 Method C guards against. Note in evidence log and flag for Gate 2 Method C interpretation.

**Status:** _[pending]_

---

### Gate 4b — Sidecar ffmpeg `-progress` fallback feasibility

**Question:** If PyAV fails (Gate 4 fails) but the HLS side is truth-anchored (Gate 2 passes), can a sidecar `ffmpeg -progress pipe:1` process produce per-frame timestamps that correspond to capture time (not output time)?

**Why it matters:** The decision tree previously listed "sidecar ffmpeg -progress" as a fallback, but `-progress` emits _output_ timestamps, not capture-time. That fallback is only valid if we can configure ffmpeg to report a capture-time-equivalent.

**Method:**
- Read ffmpeg docs and source (`fftools/ffmpeg.c`, progress reporting code path) to identify what `out_time_us` actually measures.
- Experiment: run a short ffmpeg with `-progress pipe:1` on the substream and correlate emitted `out_time_us` against a known reference (the same clock-face test from Gate 2 Method B).

**Pass criterion:** a documented, deterministic path to per-frame capture-time timestamps via a second ffmpeg subprocess.

**Fail criterion:** `-progress` only reports output timestamps with ingest-wallclock drift; no other flag produces capture-time.

**Status:** _[pending — only run if Gate 4 fails]_

## 5. Gate execution order

Strict sequence, because each step's interpretation depends on the previous step's result:

1. **Gate 1a — iMac and CloudKey NTP** (direct measurement via `ntpq -p` and CloudKey admin). This establishes the trusted local clock reference used to interpret everything afterward. A FAIL here blocks all downstream work.
2. **Gate 0 — RTCP SR capture** (uses iMac's now-verified clock as receive-time reference). A FAIL here invalidates the "RTP-anchored truth" premise and forces fallback to ingest-boundary anchoring.
3. **Gate 1b — camera-to-iMac offset** computed from Gate 0's captured SR packets. Completes Gate 1.
4. **Gate 2 — ffmpeg PDT semantics**. The scope-deciding question. If it fails, pause and re-evaluate with the user before running Gates 3 and 4.
5. **Gates 3 and 4 in parallel** — independent of each other.
6. **Gate 4b conditional** — only if Gate 4 fails AND Gate 2 passes (sidecar fallback feasibility).

## 6. Decision tree after all gates complete

Any gate ending INCONCLUSIVE → STOP, document what would unblock it, and escalate to the user. No surgery path is chosen with an INCONCLUSIVE load-bearing gate.

```
Gate 1 = FAIL
  └── STOP. Fix NTP. Rerun Gate 1 before anything else.

Gate 1 = PASS, Gate 0 = FAIL
  └── No RTCP SR available. "RTP-anchored truth" is off the table.
      Gate 2 Method A (ffmpeg source read) is still worth running — it's pure
      doc-reading, cheap, and informs whether a future firmware update that
      adds SR would unlock the small-surgery path. Method B and Method C are
      skipped (no meaningful physical reference).
      Fall back to: measure ingest-boundary offset at go2rtc (requires Gate 3).
      If Gate 3 also fails → STOP, escalate. This is a hardware/firmware problem
      that can't be solved in software alone.

Gate 1 = PASS, Gate 0 = PASS
  │
  ├── Gate 2 = PASS (PDT is RTP-anchored)
  │     └── Small surgery: rewrite frame_capture.py only.
  │         ├── Gate 4 = PASS → use PyAV. (preferred small-surgery path)
  │         ├── Gate 4 = FAIL, Gate 4b = PASS → sidecar ffmpeg -progress + merge.
  │         └── Gate 4 = FAIL, Gate 4b = FAIL → STOP, escalate; we can't stamp
  │             the pipeline side from truth without one of the two libraries.
  │
  └── Gate 2 = FAIL (PDT is ingest-anchored)
        └── Both sides currently dishonest. Per user-approved correctness-first
            ranking (a) > (b) > (c):
            (a) Replace hls_recorder.py with a Python muxer that stamps PDT
                from RTP timestamps directly.
                Requires: Gate 4 PASS (for the RTP-timestamp access).
            (b) Switch to go2rtc native HLS output, if it preserves RTP timing.
                Requires: Gate 3 PASS with demonstrable RTP-anchored PDT
                         from go2rtc's HLS output (not just API introspection).
            (c) Keep both ffmpegs. Publish a master-clock translation layer
                that converts PDT to true capture time at render time.
                Requires: Gate 3 PASS at minimum.
            If (a), (b), (c) all blocked by failed prerequisites → STOP, escalate.
```

## 7. Outputs

1. This document, with each gate's status filled in and evidence cited.
2. A single "Recommendation" section at the bottom of this doc naming the chosen surgery path.
3. If any gate ends `[INCONCLUSIVE]`, we stop and the doc explicitly names what would be needed to unblock.

## 8. Handoff

After this document is complete and the user has reviewed it, the next step is to invoke the `writing-plans` skill with the chosen surgery path. The implementation plan will cite this document for every claim it rests on.

## 8.1 Portability consideration (secondary, documented for the implementation plan)

The current system is built for specific hardware (UniFi G3 Dome cameras, CloudKey Gen 2+, iMac 2017) but the longer-term goal is to make the sync solution adaptable to any user's setup — any camera with an available stream.

Implications to carry forward into the implementation plan:

- The chosen sync strategy should **degrade gracefully** across three tiers of camera support:
  - **Tier 1 (best):** camera emits RTCP SR with NTP-disciplined timestamps → RTP-anchored truth as described in this spec.
  - **Tier 2 (medium):** camera emits no SR or untrusted SR, but go2rtc ingest is local and stable → pin truth to go2rtc ingest boundary, accept a small fixed offset we document.
  - **Tier 3 (worst):** neither — fall back to a self-calibrating measured offset, with a UI that surfaces "sync confidence: X ms" so the user knows what they're getting.
- The Gate-0-through-4 framework itself is portable: a new user should be able to run an equivalent set of gates against their camera and have the system auto-select the tier.
- Any constants that encode assumptions about UniFi hardware (RTCP SR interval, encoder buffering depth, default RTSP URL shape) should live in a single config surface, not scattered through the pipeline.
- Document the sync assumptions clearly in user-facing setup, so someone running this on a non-UniFi camera understands what sync quality they can expect.

This is explicitly secondary to the immediate "fix my iMac+UniFi dashboard" goal. Noted here so the implementation plan can consider portability as a design constraint, not retrofit it later.

## 9. Evidence log (to be filled during execution)

### Gate 0 evidence (RTCP SR presence)
_[pending]_

### Gate 1 evidence (clock ground truth)

#### Gate 1a — iMac + CloudKey direct measurement: **`[VERIFIED-TEST]` → FAIL**

**Status:** FAIL (halts verification phase per decision tree)
**Executed:** 2026-04-16 ~16:07 EDT
**Precondition snapshot:** pipeline PIDs 82750, dashboard 35104, tunnel 9302, audio 535/554, rtsp-sync scheduled. go2rtc feeder-main stream active with RTSP producer 192.168.4.9:7447. HLS manifest present at `~/bird-snapshots/hls/feeder/live.m3u8`. Postcondition snapshot matches precondition.

**iMac evidence (`sntp` to four independent NTP sources, 5 samples to time.apple.com):**

| Source | Offset | Dispersion |
|---|---|---|
| time.apple.com (5 samples) | +179.87 ms to +180.41 ms | ±17.6 to ±18.3 ms |
| pool.ntp.org | +186.41 ms | ±93.05 ms |
| time.google.com | +180.62 ms | ±24.29 ms |
| time.cloudflare.com | +180.65 ms | ±19.97 ms |
| time.nist.gov | +180.96 ms | ±55.42 ms |

Four independent upstream sources agree within 7 ms of each other. iMac is consistently **+180 ms ahead of real-time**. This is a stable offset, not measurement noise.

**`timed` daemon status:** running (PID 131, up since Tue 11:24AM boot). Log confirms active adjustments in progress (`cmd,apply,src,adjtime,...,adjust,-0.038657188,success,1` at 15:51:34) but slewing has not converged after ~2 days of uptime — drift rate exceeds macOS's conservative slew rate.

**CloudKey evidence:** REACHABLE but NOT MEASURABLE from this context.
- `ping 192.168.4.9` → 1.0 ms round-trip, reachable.
- `ssh root@192.168.4.9` → "Permission denied (publickey,keyboard-interactive)." No key auth configured.
- `https://192.168.4.9/api/status` → HTTP 401.
- `https://192.168.4.9/proxy/protect/api/nvr` → HTTP 401.

Cannot measure CloudKey offset without credentials or shell access. This is a separate unknown.

**Pass criterion required:** offset < 50 ms. **Measured:** 180 ms. **Exceeds threshold by 3.6×.**

**Interpretation:** Every downstream timing measurement in this verification phase would inherit a +180 ms bias from the iMac's wall-clock. Gate 1b's tcpdump pcap timestamps, Gate 2's PDT-vs-clock-face deltas, Gate 4's PyAV `start_time_realtime` comparisons — all anchored to a clock we now know is wrong. Proceeding would contaminate every result.

**Per Section 6 decision tree: STOP. Fix NTP. Rerun Gate 1 before anything else.**

### Gate 2 evidence (ffmpeg PDT semantics)
_[pending]_

### Gate 3 evidence (go2rtc RTCP exposure)
_[pending]_

### Gate 4 evidence (PyAV feasibility)
_[pending]_

### Gate 4b evidence (sidecar feasibility — conditional)
_[pending, conditional]_

## 10. Recommendation

_[to be written after all gates complete]_
