# Self-handoff — 2026-04-25 (pre-/compact)

**To:** post-/compact me, sometime today or tomorrow.
**From:** me, right now, with a healthy context window full of stuff that's better captured here than carried.
**Read time:** 5 minutes. Don't skip; this is your shortest path back to where we left off.

---

## ⚠️ Read these BEFORE replying to David

In this order:
1. `/Users/vives/.claude/projects/-Users-vives/memory/MEMORY.md` (auto-loaded — skim)
2. `/Users/vives/bird-classifier/docs/superpowers/specs/2026-04-25-imac-live-classify-as-built.md` — **the iMac system as it actually is, code-first.** This is the answer to most "how does X work" questions. Ground truth.
3. `/Users/vives/bird-classifier/docs/superpowers/specs/2026-04-25-hailo-playbook.md` — Hailo + Pi 5 ecosystem reference (only relevant if doing Hailo work)
4. `/Users/vives/bird-classifier/docs/superpowers/progress/2026-04-25-pi5-handoff.md` — Pi state + parallel-session protocol
5. **THIS doc** — what's open, what we agreed, where to start

You DO NOT need to re-read individual pipeline modules. The as-built doc has file:line citations for everything.

---

## Where we ARE in the conversation

We started today on Pi 5 work (overnight build verification) and pivoted multiple times. The arc:

1. **Pi work shipped** — single big commit `8cd8963` "AIY reimplement foundation: hi-res snaps, airtight review, real model switch." Track A flipped Pi to `PIPELINE_HIRES_RING=authoritative` (1080p snaps verified). Track B retired legacy `/api/review/*` POSTs and migrated UI to `/api/review2/*` with client_id idempotency (9 new contract tests, all 56 review tests green). Track C built restart-based model switch + Hailo conflict guard. Track D replaced Pi dashboard's broken live-feed with a Recent Classifications strip.

2. **Hailo playbook written** — `8cd8963` also added `2026-04-25-hailo-playbook.md`, a 14-section ecosystem reference covering HailoRT Python API, GStreamer plugins, DFC compilation flow for our future EfficientNet-Lite0 flagship, error catalog (12 codes), decision matrix, ecosystem map. Synthesized from research across 7+ sources.

3. **David's pivot 1: park Pi, fix iMac.** Reasoning: iMac is the data engine for Tier 2 training; Pi is the testing ground. We need the iMac classifier producing clean training data before any cleanlab/Tier 2 work.

4. **I almost made a bad guess about iMac NTP.** Cited `OVERLAY_LEAD_COMPENSATION_MS = 1000` as if it were active. David called me out — overlay sync is internally consistent (both stamps come from Python `time.time()` on the iMac, same OS clock = cancels regardless of NTP truth). I had been working from stale spec memory.

5. **David's pivot 2: become THE expert on the iMac system. Read the code, not the docs.** Did that. Read everything in `pipeline/`, `bird_inference.py`, `dashboard/live.html` (full), `dashboard/api.py` HLS routes, plus `~/docs/bird-observatory/22-gotchas.md` (selectively). Wrote `2026-04-25-imac-live-classify-as-built.md` as the new canonical reference. Commit `8d79e70`.

6. **David's pivot 3: write a Pi handoff for parallel session.** Did that. Same commit `8d79e70`. That doc tells parallel-Claude what's theirs vs mine, what's open on Pi side, and the coordination protocol.

7. **David's pivot 4: clean up the docs that confused me.** Did that. Stamps on 4 stale docs + canonical-reference section in README. Commits `7916d3c` (bird-classifier specs) and `fafaa56` (~/docs/bird-observatory).

8. **David called me out for not using subagents and not invoking skills.** Right call. I dispatched a general-purpose agent for the doc cleanup batch (3 edits in parallel). I also invoked `superpowers:writing-plans` to confirm I was using skills. Lesson: **invoke skills early, dispatch parallel work to subagents.**

9. **David asked for self-handoff + /compact prompt.** This doc + `2026-04-25-compact-prompt-v2.md`. Then we /compact.

---

## Network fact (David corrected me)

iMac and Pi are **both wired to the same LAN**. No Tailscale needed for any
iMac↔Pi communication (rsync, ssh, curl, etc). Pi is reachable at `pi5.local`
from iMac and vice versa. iMac's LAN IP is `192.168.4.200`. The earlier
version of the Pi handoff suggested rsync over Tailscale — that's been fixed.

## What's settled (don't re-litigate)

- **Snapshot architecture decision: still OPEN.** I proposed extracting hi-res frames from the existing HLS recorder buffer (`segments.json` + on-demand .ts decode) instead of running the redundant 1080p ring buffer ffmpeg. David hasn't said go/no-go. Pi has the ring on (it's a testing ground); iMac has neither ring nor the proposed HLS-extract path — it's still on cheap restore (640×360 snaps).
- **iMac overlay sync is NTP-independent BY DESIGN** — both clocks (SSE `wall_time_ms` and segment `completed_ms`) are stamped by Python `time.time()` on the iMac at corresponding pipeline stages. No band-aids. No compensation. Read `as-built.md §2` for the full reconciliation.
- **iMac DB labels are AIY's, not yard's.** `authoritative_classify()` in SnapshotWriter overrides yard's live-UX label with AIY's 965-species call before the row hits classifications.db. So pre-Sonoma 69.3% top-1 / 75.6% macro-F1 baseline is preserved on iMac.
- **iMac code edits from this session are LIVE** in `/Users/vives/bird-classifier/` BUT iMac dashboard hasn't been restarted to pick up the new airtight review code. Whether to restart is a David decision (do it during Tier 2 prep, not now while Pi is the testing ground).
- **Pi has Hailo-classifier conflict guard.** Pipeline-view registry excludes hailo classifiers (`exclude_hailo=True`) because Hailo-8L has 1 vdevice and YOLO detector holds it. Lab registry keeps them for upload-test. Multi-model fix path is documented in playbook §9 Path 1.
- **Parallel-session handoff is shipped.** `2026-04-25-pi5-handoff.md`. If David spins up parallel-Claude, it tells them what to read first.

---

## What's OPEN (in priority order)

### 1. Snapshot upgrade path proposal — DO NEXT after /compact

David asked the question; I outlined the trade-offs in the as-built doc §9 but never wrote a formal plan or got David's go/no-go.

The proposal: build `pipeline/hls_frame_picker.py` that, given `wall_time_ms`, walks `segments.json`, opens the right .ts via PyAV, seeks to the offset, decodes one (or k) frames, returns numpy BGR. Replace SnapshotWriter's `_pick_from_ring` with this. Keep ring code as fallback / dev tool for now.

Wins: zero continuous CPU on iMac (vs ~30-50% with ring), 30fps native frame pool (vs 5fps throttled), snapshot frame is byte-identical to displayed video frame (eliminates "doesn't match what I clicked on" bugs), uses same clock as overlay (consistent).

Cost: ~50-200ms decode latency per snapshot (background thread, fine since not on hot path).

**On /compact resume, invoke `superpowers:writing-plans` and write the implementation plan.** The plan is a code change in 1-2 files + tests + verification steps. Not a research project.

### 2. Decide whether to enable on iMac

After the plan is written: actually flip iMac. This is a couple of changes:
- New `pipeline/hls_frame_picker.py` (~100 lines)
- `pipeline/snapshot_writer.py` modified to use it
- Tests
- Restart `bird-pipeline` LaunchAgent on iMac
- Verify next 10 snapshots are 1920×1080

Not a Pi change. Pi keeps the ring buffer until we verify the HLS-extract path works on the iMac side.

### 3. Tier 2 Phase 1: cleanlab on the 34K weak AIY labels

Per `2026-04-23-tier2-training-plan-v1.md` §Step 2. Cross-fold train 3 EfficientNet-Lite0 models on 34K AIY-labeled data, get out-of-fold predictions, run `cleanlab.filter.find_label_issues`, prune 10-30%. Needs x86 GPU (cloud VM). David has not greenlit yet — the iMac data integrity gates above are the prerequisite.

### 4. Pi thermal watch (24h)

Pi sustained 83-86°C with fan at max during ring buffer authoritative + YOLO + AIY-on-CPU load. Set up a cron/systemd timer logging temp + freq every minute for 24h, then decide whether to drop ring fps from 5 to 3. Could be parallel-Claude's job.

### 5. iMac UI restart to pick up airtight review code

The new `/api/review2/batch-confirm`, `batch-reject`, `rerun-missed`, `second-opinion/{file}` endpoints + UI migration are in iMac repo files. iMac dashboard hasn't been restarted. When ready: `launchctl kickstart -k gui/$(id -u)/com.vives.bird-dashboard`. Not urgent — David's call.

---

## How David is working with me right now

Three observed patterns from this session:

1. **He pushes back when I'm operating from stale memory or generic frameworks.** "you literally built this with me and you forgot... there is a git, there is the codebase itself. get to know it" — that was the kick I needed. **The code is the ground truth. Always.**

2. **He pushes back when I'm not using available tools.** "why arent you using subagents for any of this. do you see your available skills? dont any apply?" — invoke skills early, dispatch parallel work to subagents, don't grind sequentially.

3. **He's strategic about session arc.** Repeatedly tells me to write handoff docs / compact prompts BEFORE we hit compression. That foresight is gold; respect it.

What works for him:
- Honest correction when I'm wrong (he ack'd both my misreads quickly)
- Tight responses, not over-explaining
- Real evidence (code citations, command output) over assertions
- Skills/subagents over solo grind

What annoys him:
- Asking questions I should be able to answer from the code
- Citing docs as truth without verifying against code
- Long preambles before action

---

## Skills that DEFINITELY apply on resume

- `superpowers:writing-plans` — when writing the snapshot upgrade plan (the next concrete task). Plans live at `docs/superpowers/plans/2026-04-25-hls-frame-picker.md` per the skill's convention.
- `superpowers:test-driven-development` — when implementing the picker (tests first, see plan).
- `superpowers:verification-before-completion` — always-on. Don't claim "snapshots now hi-res" without curl'ing the next saved JPG and confirming dimensions.
- `superpowers:debugging` — if anything in the picker breaks (watch for HAILO/ffmpeg/PyAV errors).

Probably NOT needed:
- brainstorming — David's already chosen the path (HLS-extract); no need to re-ideate
- writing-handoffs (if it exists) — already done

---

## File pointers (the only ones that matter)

| What | Path |
|---|---|
| iMac as-built | `~/bird-classifier/docs/superpowers/specs/2026-04-25-imac-live-classify-as-built.md` |
| Hailo playbook | `~/bird-classifier/docs/superpowers/specs/2026-04-25-hailo-playbook.md` |
| Pi 5 handoff | `~/bird-classifier/docs/superpowers/progress/2026-04-25-pi5-handoff.md` |
| THIS handoff | `~/bird-classifier/docs/superpowers/progress/2026-04-25-self-handoff.md` |
| /compact prompt | `~/bird-classifier/docs/superpowers/progress/2026-04-25-compact-prompt-v2.md` |
| Tier 2 training plan | `~/bird-classifier/docs/superpowers/specs/2026-04-23-tier2-training-plan-v1.md` |
| Tier 2 data audit | `~/bird-classifier/docs/superpowers/specs/2026-04-23-tier2-data-audit.md` |
| Snapshot writer (the file we'll modify) | `~/bird-classifier/pipeline/snapshot_writer.py` |
| HLS recorder (we'll read its segments.json output) | `~/bird-classifier/pipeline/hls_recorder.py` |

---

## TL;DR for the first 60 seconds post-/compact

```
1. cat ~/.claude/projects/-Users-vives/memory/MEMORY.md  (auto-loaded; just acknowledge)
2. Read 2026-04-25-imac-live-classify-as-built.md  (5 min — code-level reality)
3. Read this self-handoff doc  (you're here)
4. To David: "Resumed. Caught up via the as-built + self-handoff. Ready to write the snapshot upgrade plan via writing-plans skill — go?"
5. If yes: invoke writing-plans, save to docs/superpowers/plans/2026-04-25-hls-frame-picker.md.
```

Don't propose anything new until you've read the as-built. Don't ask David questions you could answer from the code. Don't grind solo when a subagent + skill would be faster.

You've got this. The hard work of building system understanding is already done.
