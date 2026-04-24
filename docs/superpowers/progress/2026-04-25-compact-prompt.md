# /compact prompt for the Pi 5 overnight → next session transition

Copy-paste this as the argument to `/compact` in Claude Code. It biases the
summary toward what matters after the overnight Pi 5 build, and tells
post-compaction Claude which files to read first.

---

```
Focus the summary on: the Pi 5 overnight build (2026-04-24 → 25) AND
the classifier trajectory.

🚩 MUST PRESERVE — David said right before /compact:
  "we took a pi5 detour but i still want the aiy reimplement to finish"
The "AIY reimplement" is an IN-PROGRESS classifier work stream that
precedes the Pi 5 detour. Most likely refers to the Tier 2 flagship
training (the replacement for AIY), possibly to Model-Lab→pipeline
live hot-swap, possibly to AIY-on-Hailo compile. On session resume,
ask David a one-sentence clarifier, then continue.

Also preserve concrete state — what services are running on the Pi,
what got committed, what's deferred — and the decision rationales
(Coral-free, AIY-on-CPU via ONNX, YOLOv8s on Hailo, 4 themes,
Model Lab is Lab-only not pipeline-hot-swap).

The revamp BEFORE the Pi port (2026-04-22 → 24: Live tab fix, 0b cull,
1a integrity audit, 1b ring buffer, airtight review backend, cheap
restore, /work page, /review-ideas mockup, Tier 2 training plan,
evaluation harness) is captured in memory files + commits — do NOT
re-summarize those in detail; one-line reference each.

READ THESE ON THE OTHER SIDE before doing anything, in order:
  1. ~/.claude/projects/-Users-vives/memory/project_pi5_overnight_build.md
     (state file — services, deferred items, key commits)
  2. ~/.claude/projects/-Users-vives/memory/project_classifier_state.md
     (classifier trajectory, the AIY-reimplement callout, decisions made)
  3. ~/bird-classifier/docs/superpowers/progress/2026-04-25-morning-brief.md
     (what David sees when he wakes up)
  4. ~/.claude/projects/-Users-vives/memory/MEMORY.md
     (the full index)
  5. ~/bird-classifier/docs/superpowers/progress/2026-04-24-pi5-overnight.md
     (live progress log from the overnight)

Compress aggressively on everything else — iMac memory files, lit
reviews, training plan details, review-system airtight internals —
those live in their own docs; point at them, don't inline them.

Do NOT lose: systemd service names on Pi, the pi5.vivessato.com tunnel
UUID (bf725288-989b-4ae4-9d71-ea457310a8d4), PI_MODE=1 env gate,
commit hashes (aa033e6 is the big Pi 5 build; 32863b7 is the brief),
AND David's "aiy reimplement" callout.
```

---

## Why this shape

- **Biases** the summary toward the current session so recency wins.
- **Delegates** all pre-Pi-5 detail to docs on disk.
- **Anchors** the four must-read files by absolute path so the next turn can load them immediately.
- **Lists** the non-negotiable technical facts (service names, tunnel UUID, env gates, commit hashes) that are cheap to keep but expensive to lose.

## What post-compaction Claude should do first

1. Read the four files listed.
2. Check `git log --oneline | head -10` to see what commits are on `main`.
3. Check `ssh vives@pi5.local "systemctl --user status bird-pipeline bird-dashboard go2rtc cloudflared --no-pager"` to see Pi health.
4. Read the TodoWrite list — every completed item there is real.
5. Then ask David what's next, OR continue the deferred list from the Pi 5 brief.
