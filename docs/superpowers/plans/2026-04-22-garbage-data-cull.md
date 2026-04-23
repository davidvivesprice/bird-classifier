# Garbage Data Cull (Tier 0b) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans.

**Goal:** Remove the ~10,600 classifications produced since the Sonoma-migration hi-res re-crop path landed (2026-04-19 21:16) that are contaminated by the stale-bbox hallucination bug — move their JPGs to a dated quarantine dir, drop their DB rows, preserve any that already carry a human review.

**Why now:** Those rows are confidently-wrong classifications on empty or partial-bird crops. Keeping them in the DB poisons: (a) the reviewer's queue (wastes David's time), (b) any Tier-2 training set that reads the `classifications` table, (c) the dashboard's species tiles and daily rhythm. They must leave before the hallucination is fixed (1b), else we'll re-pollute as we go.

**Architecture:** Two-phase cull with a 30-day grace period. Phase 1 (this plan) identifies rows, moves JPGs to `~/bird-snapshots/culled/2026-04-22/`, and marks the DB rows with an `action='culled_hallucination'` tombstone (preserves row for audit, hides from dashboard queries). Phase 2 (not this plan — scheduled 30 days out) hard-deletes culled rows + their quarantined JPGs.

**Rows touched:**
- Scope: `source_timestamp >= '2026-04-19T21:16'` AND NOT EXISTS(reviews row for this file). Verified count at recon: **10,622 rows out of 10,684 in the window** — 62 rows have reviews and stay put.
- Of those 10,622, all get JPG moves + action tombstone.
- Any row whose JPG already doesn't exist on disk gets logged as pre-orphaned (feeds 1a's root-cause hunt).

**Tech Stack:** Python 3.12 (venv/), sqlite3, `classifications_db` module. No ML, no network calls.

---

## File Structure

**Files created:**
- `~/bird-classifier/tools/cull_hallucination_window.py` — one-shot script with dry-run + execute modes.
- `~/bird-snapshots/culled/2026-04-22/` — quarantine dir created by the script.
- `~/bird-classifier/tools/cull_hallucination_window.log` — per-row cull decisions with reasons.

**Files NOT modified:** `classifications.db` schema unchanged; only row `action` values flipped.

---

### Task 1: Verify the cutoff timestamp against code history

**Goal:** confirm the hi-res re-crop path really started at 2026-04-19 21:16, not earlier.

- [ ] **Step 1.1: Confirm snapshot_writer.py has not been committed since the mtime**

  Run:
  ```bash
  cd ~/bird-classifier
  git log --all --format="%h %ci %s" -- pipeline/snapshot_writer.py | head -5
  stat -f "%Sm %N" pipeline/snapshot_writer.py
  ```
  Expected: only one commit in log (`8120d07 WIP: smooth overlay...`); mtime shows 2026-04-19 ~21:16.

- [ ] **Step 1.2: Spot-check the transition in the DB**

  ```bash
  sqlite3 ~/bird-snapshots/logs/classifications.db "
  SELECT source_timestamp, common_name, file FROM classifications
  WHERE source_timestamp BETWEEN '2026-04-19T20:00' AND '2026-04-19T23:00'
  ORDER BY source_timestamp LIMIT 40;
  "
  ```
  Look for the earliest row whose `file` prefix matches the new pattern and whose species starts showing the hallucination drift (big species variety on same camera inside minutes). That's the true cutoff.

  Evidence gate: the cutoff timestamp chosen for Task 2 is either `2026-04-19T21:16` OR the earlier row from Step 1.2 if the transition started before the mtime. Record the chosen value.

---

### Task 2: Write the cull script (dry-run mode first)

**Files:**
- Create: `~/bird-classifier/tools/cull_hallucination_window.py`

- [ ] **Step 2.1: Write the script**

  ```python
  #!/usr/bin/env python3
  """Cull the post-2026-04-19 hallucination window from classifications.db.

  Moves affected JPGs to ~/bird-snapshots/culled/<today>/, marks DB rows
  action='culled_hallucination', logs every decision to
  tools/cull_hallucination_window.log.

  Usage:
      python3 tools/cull_hallucination_window.py --dry-run   # default
      python3 tools/cull_hallucination_window.py --execute   # actually do it
  """
  import argparse
  import datetime
  import logging
  import shutil
  import sqlite3
  import sys
  from pathlib import Path

  DB = Path.home() / "bird-snapshots" / "logs" / "classifications.db"
  SNAPSHOTS = Path.home() / "bird-snapshots"
  CLASSIFIED = SNAPSHOTS / "classified"
  ANNOTATED = SNAPSHOTS / "annotated"
  PENDING = SNAPSHOTS / "pending"
  CULL_ROOT = SNAPSHOTS / "culled" / datetime.date.today().isoformat()
  # See Task 1 — override with --cutoff if recon adjusts.
  DEFAULT_CUTOFF = "2026-04-19T21:16"

  LOG = Path(__file__).parent / "cull_hallucination_window.log"

  def find_jpg(file: str) -> Path | None:
      """Search the known snapshot dirs for the JPG."""
      # classified/<species>/<file>
      for species_dir in CLASSIFIED.glob("*"):
          p = species_dir / file
          if p.exists():
              return p
      # annotated/<file>
      p = ANNOTATED / file
      if p.exists():
          return p
      # pending/<file>
      p = PENDING / file
      if p.exists():
          return p
      return None

  def main():
      ap = argparse.ArgumentParser()
      ap.add_argument("--execute", action="store_true",
                      help="actually move files + update DB; otherwise dry-run")
      ap.add_argument("--cutoff", default=DEFAULT_CUTOFF,
                      help="ISO source_timestamp; rows >= this are candidates")
      args = ap.parse_args()

      logging.basicConfig(
          filename=LOG, level=logging.INFO,
          format="%(asctime)s %(levelname)s %(message)s",
      )
      log = logging.getLogger(__name__)

      conn = sqlite3.connect(str(DB))
      conn.row_factory = sqlite3.Row
      # Rows in window AND not reviewed. Reviews live in a separate table —
      # LEFT JOIN; keep only rows with no review.
      cur = conn.execute("""
          SELECT c.id, c.file, c.common_name, c.source_timestamp, c.action
          FROM classifications c
          LEFT JOIN reviews r ON r.file = c.file
          WHERE c.source_timestamp >= ?
            AND r.file IS NULL
            AND c.action != 'culled_hallucination'
          ORDER BY c.source_timestamp
      """, (args.cutoff,))
      rows = cur.fetchall()
      log.info("scope: %d rows >= %s, unreviewed", len(rows), args.cutoff)
      print(f"scope: {len(rows)} rows >= {args.cutoff}, unreviewed")

      moved = missing = errors = 0
      CULL_ROOT.mkdir(parents=True, exist_ok=True) if args.execute else None

      for row in rows:
          jpg = find_jpg(row["file"])
          if jpg is None:
              missing += 1
              log.info("pre-orphan: id=%d file=%s (no jpg found on disk)",
                       row["id"], row["file"])
              if args.execute:
                  conn.execute(
                      "UPDATE classifications SET action='culled_hallucination' WHERE id=?",
                      (row["id"],))
              continue

          # also move annotated/<file> if present
          ann = ANNOTATED / row["file"]
          targets = [jpg] + ([ann] if ann.exists() else [])
          if args.execute:
              try:
                  for src in targets:
                      dst = CULL_ROOT / src.name
                      # If two files collide (classified + annotated have same basename),
                      # prefix with parent dir.
                      if dst.exists():
                          dst = CULL_ROOT / f"{src.parent.name}__{src.name}"
                      shutil.move(str(src), str(dst))
                  conn.execute(
                      "UPDATE classifications SET action='culled_hallucination' WHERE id=?",
                      (row["id"],))
                  moved += 1
                  log.info("culled: id=%d file=%s species=%s",
                           row["id"], row["file"], row["common_name"])
              except Exception as e:
                  errors += 1
                  log.exception("error culling id=%d: %s", row["id"], e)
          else:
              moved += 1  # what would be moved

      if args.execute:
          conn.commit()
      conn.close()

      mode = "EXECUTE" if args.execute else "DRY-RUN"
      summary = (
          f"\n[{mode}]\n"
          f"  would-cull rows:   {moved}\n"
          f"  pre-orphan rows:   {missing}\n"
          f"  errors:            {errors}\n"
          f"  log:               {LOG}\n"
          f"  quarantine dir:    {CULL_ROOT}\n"
      )
      print(summary)
      log.info(summary.strip())


  if __name__ == "__main__":
      sys.exit(main())
  ```

- [ ] **Step 2.2: Run dry-run, read output, show David**

  ```bash
  cd ~/bird-classifier
  python3 tools/cull_hallucination_window.py --dry-run
  ```
  Expected output: ~10,600 would-cull; some small number of pre-orphans (these are the 1a smoking-gun candidates — log captures their filenames).

  Evidence gate: David reviews the dry-run summary. The pre-orphan number is the interesting one — it quantifies how many rows are ALREADY orphaned pre-cull (confirms orphan-cull infra was never actually built, or is broken).

---

### Task 3: Execute + verify

- [ ] **Step 3.1: Execute cull**

  ```bash
  cd ~/bird-classifier
  python3 tools/cull_hallucination_window.py --execute
  ```
  Expected: summary matches dry-run within rounding.

- [ ] **Step 3.2: Sanity-check the DB after cull**

  ```bash
  sqlite3 ~/bird-snapshots/logs/classifications.db "
  SELECT action, COUNT(*) FROM classifications
  WHERE source_timestamp >= '2026-04-19T21:16'
  GROUP BY action;
  "
  ```
  Expected: `culled_hallucination | ~10622`, `classified | ~62` (the reviewed ones that stayed).

- [ ] **Step 3.3: Sanity-check the quarantine dir**

  ```bash
  ls ~/bird-snapshots/culled/$(date +%F) | head -5
  echo "count:"
  ls ~/bird-snapshots/culled/$(date +%F) | wc -l
  ```
  Expected: many files, count within ~2× the `moved` total (classified JPG + annotated JPG per row).

- [ ] **Step 3.4: Dashboard smoke test**

  David reloads the dashboard:
  - Activity tab should show a clean break at 2026-04-19 — the recent-classifications list should only show reviewed rows from the window (or nothing from the window).
  - Species tiles should drop their hallucinated counts (e.g., "American Robin" count should go down dramatically if Robin was the main hallucination).

  Evidence gate: David confirms the dashboard looks honest — no more obvious hallucinations in "Recently Seen."

- [ ] **Step 3.5: Commit the tool**

  ```bash
  cd ~/bird-classifier
  git add tools/cull_hallucination_window.py
  git commit -m "$(cat <<'EOF'
  Add hallucination-window cull tool

  Moves JPGs from the 2026-04-19+ stale-bbox window to a dated quarantine
  dir and marks their DB rows action='culled_hallucination'. Reviewed
  rows are preserved. Script supports --dry-run (default) and --execute.
  Run output captured in tools/cull_hallucination_window.log for audit.

  Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
  EOF
  )"
  ```

---

### Task 4: Document the 30-day hard-delete follow-up

- [ ] **Step 4.1: Add a forget-me-not**

  Append to `~/.claude/projects/-Users-vives/memory/project_forget_me_nots.md` under Data & Training:

  ```markdown
  - [ ] **Hard-delete hallucination-window quarantine** — after 2026-05-22, verify
    `~/bird-snapshots/culled/2026-04-22/` is no longer needed (David has not
    requested any row reinstated), then `rm -rf` that dir and
    `DELETE FROM classifications WHERE action='culled_hallucination'`. One-liner.
  ```

---

## Self-review notes

- **Spec coverage:** cutoff identification (Task 1), cull (Task 2-3), retention (Task 4).
- **Placeholder scan:** none — script is complete.
- **Safety:** dry-run default; quarantine instead of delete; reviewed rows preserved; DB tombstone instead of row delete (Phase 2 finishes the job 30 days later).
- **Pre-orphan count** surfaced by the script is the *input* to 1a's debug-tooling scope — don't build 1a without that number.
