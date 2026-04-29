> **HISTORICAL** — This document describes a design, plan, or system state that has been superseded. Kept for decision-trail context.

# /compact prompt — 2026-04-25 v2 (the iMac live-classify mastery session)

Copy-paste the ```block``` below into `/compact` to bias the summary toward what
matters and tell post-compact-Claude where to read first.

---

```
Focus the summary on: (1) the iMac live-classify subsystem mastery
arc — went from "operating from stale memory" to "wrote the
canonical as-built spec from code-first reading" — and (2) the
snapshot upgrade decision that's still open (hi-res ring buffer
duplicate decode vs harvesting from the existing HLS recorder
buffer via segments.json + on-demand .ts decode).

🚩 MUST PRESERVE — David said this twice with rising frustration:

1. "you literally built this with me and you forgot... there is a git,
   there is the codebase itself. get to know it"
   → Translation: docs may be stale. Code is ground truth. Always.
   The as-built spec at
   docs/superpowers/specs/2026-04-25-imac-live-classify-as-built.md
   is the new canonical reference, written code-first with file:line
   citations. Trust IT, not the older numbered docs in ~/docs/.

2. "why arent you using subagents for any of this. do you see your
   available skills? dont any apply?"
   → Translation: invoke skills early, dispatch parallel work to
   subagents, don't grind solo. THIS APPLIES IMMEDIATELY when post-
   compact Claude resumes — the next concrete task is writing the
   snapshot upgrade plan, which means invoking
   superpowers:writing-plans.

3. The overlay sync is INTERNALLY CONSISTENT BY DESIGN, NOT a
   band-aid. Both the SSE wall_time_ms and the segment completed_ms
   are stamped by Python time.time() on the iMac at corresponding
   pipeline stages — same OS clock, cancels regardless of NTP truth.
   The "OVERLAY_LEAD_COMPENSATION_MS = 1000" reference Claude cited
   earlier was a phantom from a SUPERSEDED April 16 verification
   spec. That band-aid has not been in the code since the April 17
   pivot to the sidecar manifest. Don't bring it up again.

ALSO PRESERVE the conceptual journey of the snapshot question:
  - Pi has PIPELINE_HIRES_RING=authoritative live (1080p snaps,
    verified). Costs ~30-50% extra CPU on Pi via second 1080p decode.
  - iMac is still on cheap restore (640×360 snaps). Ring buffer is
    env-gated off because iMac is i5-7400 8GB and second decode is
    expensive.
  - David asked "is hi-res snaps unnecessary? what are the models
    expecting?" Answer: AIY/EfficientNet-Lite0 ingest 224×224, but
    bigger source crop downsampled > small source crop upsampled.
    Hairy/Downy specialist needs the eye/feather detail. Hi-res snaps
    are RIGHT for training data quality.
  - Then David: "we have a buffer already set up to line up the
    bounding boxes with the hi res stream, which was like 5 days of
    setup. that system may yield opportunities for this hi res
    snapshot to be inserted." → He's pointing at the HLS recorder +
    segments.json sidecar that powers the /live overlay alignment.
    Hi-res .ts segments are ALREADY on disk with Python-stamped
    completion times. We can extract one frame on demand instead of
    running a parallel ring decode.
  - Proposed plan (NOT YET WRITTEN, NOT YET APPROVED): build
    pipeline/hls_frame_picker.py — given wall_time_ms, find the .ts
    segment containing that moment, open via PyAV, seek to offset,
    decode one (or k) frames, return numpy BGR. Replace
    SnapshotWriter._pick_from_ring. Wins: zero continuous CPU on
    iMac, 30fps native pool, snapshot byte-identical to displayed
    video frame, same clock as overlay.
  - Cost: ~50-200ms decode latency per snapshot (background thread,
    fine — snapshots not on hot path).

NEXT CONCRETE TASK on resume: invoke superpowers:writing-plans skill
and write the implementation plan for hls_frame_picker. Save to
docs/superpowers/plans/2026-04-25-hls-frame-picker.md per the skill
convention.

Also preserve concrete state — what services are running on Pi
(go2rtc, bird-pipeline, bird-dashboard, cloudflared, all systemd-user
linger active), what's in classifier registry on Pi (aiy_onnx active;
hailo classifiers excluded from pipeline-view registry by
exclude_hailo=True due to Hailo-8L 1-vdevice limit; multi-model fix
documented as Path 1 in the Hailo playbook §9), what's in the env
file (PI_MODE=1, PIPELINE_HIRES_RING=authoritative,
PI_CLASSIFIER=aiy_onnx, plus UNIFI_API_KEY).

Compress aggressively on:
  - Individual file reads I did during the as-built research
    (process_thread.py, snapshot_writer.py, hls_recorder.py, etc.) —
    the as-built spec captured everything that mattered with citations
  - Older specs (April 15-16 overlay design + verification) — they're
    now stamped SUPERSEDED in their headers, no need to inline
  - Pi 5 overnight session work — captured in 2026-04-25-pi5-handoff.md
  - The Hailo research — captured in 2026-04-25-hailo-playbook.md

READ THESE ON THE OTHER SIDE before doing anything, in order:
  1. ~/.claude/projects/-Users-vives/memory/MEMORY.md (auto-loaded)
  2. ~/bird-classifier/docs/superpowers/progress/2026-04-25-self-handoff.md
     (THE shortest path back to where we left off)
  3. ~/bird-classifier/docs/superpowers/specs/2026-04-25-imac-live-classify-as-built.md
     (the iMac system as it actually is — code-first, file:line citations)
  4. ~/bird-classifier/docs/superpowers/specs/2026-04-25-hailo-playbook.md
     (only if doing Hailo work)
  5. ~/bird-classifier/docs/superpowers/progress/2026-04-25-pi5-handoff.md
     (only if working on Pi side or coordinating with parallel session)

Do NOT lose:
  - The as-built spec is the new canonical reference; numbered docs in
    ~/docs/bird-observatory/ are now stamped with STATUS banners where
    stale (08, 30, 30a) and superseded specs in superpowers/specs (April
    15, April 16) carry their own banners
  - Commit hashes: 8cd8963 = the big Pi shipped commit, 8d79e70 = the
    two grounding docs (as-built + handoff), 7916d3c = April 15-16 spec
    stamps, fafaa56 = ~/docs/bird-observatory cleanup
  - Pi tunnel UUID bf725288-989b-4ae4-9d71-ea457310a8d4 → pi5.vivessato.com
  - The 1a integrity audit runs on iMac as a LaunchAgent
    (com.vives.bird-integrity-audit), I haven't checked recent runs
  - Parallel-session protocol: if David spawns parallel-Claude on Pi,
    the handoff doc tells them what's theirs, what's mine, and the
    coordination rules (PI_MODE-gate everything, edit on iMac side
    rsync to Pi, ask David to mediate shared-file conflicts)
  - David's working style observations (in self-handoff §"How David
    is working with me right now"): pushes back hard on stale memory,
    pushes back hard on solo grind, strategic about session-arc
    handoffs. Respect the foresight; he's been right both times.
```

---

## Why this shape

- **Biases** the summary toward the conceptual journey (mastery arc + open
  decision) over the file-level work (which the as-built doc now captures).
- **Anchors** the five must-read files by absolute path, with the self-handoff
  as the FIRST READ — that's the doc that re-orients you fastest.
- **Calls out** the meta-lessons (use skills, use subagents, code is ground
  truth) so post-compact Claude doesn't repeat the same mistakes I did this
  session.
- **Specifies** the next concrete task (writing-plans skill → snapshot upgrade
  plan) so resumption is action, not analysis paralysis.
- **Lists** the non-negotiable facts (Pi state, env vars, commit hashes,
  conflict guard) that are cheap to keep and expensive to lose.
- **Aggressive compression** on file-read transcripts because the as-built
  doc is the higher-density distillation.

## What post-/compact Claude should do FIRST

1. Read the self-handoff doc (5 minutes — it has a 60-second TL;DR at the
   bottom).
2. Glance at the as-built doc TOC.
3. Reply to David: "Resumed. Caught up via self-handoff + as-built. Ready to
   invoke writing-plans skill for the snapshot upgrade plan — go?"
4. If yes: invoke `superpowers:writing-plans`, write
   `docs/superpowers/plans/2026-04-25-hls-frame-picker.md`.
5. If no: ask what's next; don't guess.