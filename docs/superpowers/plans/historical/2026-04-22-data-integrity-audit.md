> **HISTORICAL** — This document describes a design, plan, or system state that has been superseded. Kept for decision-trail context.

# Data-Integrity Audit + Orphan Cull (Tier 1a) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans.

**Goal:** Build the orphan-row audit that was specced in March but never implemented. For every row in `classifications.db`, verify the referenced JPG exists in a valid location; log the divergence; cull the orphan. Same script reverse-direction: files on disk without a DB row → flagged (not deleted — David hand-reviews after first run).

**Why it matters:** The 0b dry-run surfaces the first hard count of pre-existing orphans. David has said the orphan bug has "bitten more than a few times." Without instrumentation that *catches* it on every run, we'll keep seeing it come back. This plan delivers: (a) one-shot audit + cull, (b) a recurring integrity check that runs hourly, (c) structured logging so the next occurrence exposes its root cause immediately.

**Architecture:** Two components.
1. **`tools/audit_data_integrity.py`** — idempotent auditor. Enumerates every `classifications` row, walks the known snapshot directories, builds a set-difference report. `--cull` flag actually removes orphans; default is dry-run.
2. **Hourly systemd/launchd invocation** — adds a LaunchAgent that runs the auditor with `--cull` every hour, logs to `~/Library/Logs/bird-audit.log`. If the orphan count jumps, the log shows which rows + when they were written → traceable to pipeline state.

Plus a provisional hypothesis for WHY orphans happen: `SnapshotWriter._write_one` (lines 402–411) deletes the main JPG if `insert_classification` raises — but does NOT delete the annotated JPG written at 352–361. Inverse failure (JPG write succeeds, then DB write succeeds, but some later step fails) is not currently possible per the code, so the orphan root cause is likely elsewhere — possibly `apply_verdict` in api.py moving files without updating the row. The auditor's logs will tell us.

**Tech Stack:** Python 3.12, sqlite3, launchd, no external dependencies.

---

## File Structure

**Files created:**
- `~/bird-classifier/tools/audit_data_integrity.py` — the auditor.
- `~/Library/LaunchAgents/com.vives.bird-integrity-audit.plist` — hourly agent.
- `~/Library/Logs/bird-audit.log` — agent stdout/stderr.

**Files NOT modified:** DB schema unchanged; pipeline code unchanged (diagnosis-only for this tier).

---

### Task 1: Write the auditor script

**Files:**
- Create: `~/bird-classifier/tools/audit_data_integrity.py`

- [ ] **Step 1.1: Write script**

  ```python
  #!/usr/bin/env python3
  """Verify every classifications.db row has a JPG on disk, and vice-versa.

  Usage:
      python3 tools/audit_data_integrity.py                 # report only
      python3 tools/audit_data_integrity.py --cull          # delete orphan DB rows
      python3 tools/audit_data_integrity.py --json          # machine-readable output
  """
  import argparse
  import datetime
  import json
  import logging
  import sqlite3
  import sys
  from pathlib import Path

  SNAPSHOTS = Path.home() / "bird-snapshots"
  DB = SNAPSHOTS / "logs" / "classifications.db"
  CLASSIFIED = SNAPSHOTS / "classified"
  ANNOTATED = SNAPSHOTS / "annotated"
  PENDING = SNAPSHOTS / "pending"
  TRASH = SNAPSHOTS / "trash"
  CULLED = SNAPSHOTS / "culled"

  # Valid "file exists somewhere" locations, in the order we prefer.
  SEARCH_ROOTS = [CLASSIFIED, ANNOTATED, PENDING, TRASH, CULLED]

  LOG = Path(__file__).parent / "audit_data_integrity.log"


  def build_disk_index() -> dict[str, Path]:
      """Return {filename: first-found path} across all known snapshot roots."""
      index: dict[str, Path] = {}
      for root in SEARCH_ROOTS:
          if not root.exists():
              continue
          for p in root.rglob("*.jpg"):
              index.setdefault(p.name, p)  # first hit wins — preference order above
      return index


  def audit(conn: sqlite3.Connection) -> dict:
      disk = build_disk_index()
      cur = conn.execute("""
          SELECT id, file, common_name, action, source_timestamp
          FROM classifications
          WHERE action IN ('classified', 'no_bird')
            AND action != 'culled_hallucination'
      """)
      rows = cur.fetchall()

      db_files = {r["file"] for r in rows}
      orphan_rows = []   # DB row with no jpg
      for r in rows:
          if r["file"] not in disk:
              orphan_rows.append({
                  "id": r["id"],
                  "file": r["file"],
                  "species": r["common_name"],
                  "source_timestamp": r["source_timestamp"],
                  "action": r["action"],
              })

      orphan_files = []  # jpg with no DB row (informational only — don't delete)
      for fname, path in disk.items():
          if fname not in db_files and CLASSIFIED in path.parents:
              # Only flag files under classified/ — annotated/ intentionally
              # mirrors classified/; trash/ and culled/ are expected to have
              # files without matching rows.
              orphan_files.append(str(path))

      return {
          "total_rows_checked": len(rows),
          "disk_files_found": len(disk),
          "orphan_rows": orphan_rows,
          "orphan_files_count": len(orphan_files),
          "orphan_files_sample": orphan_files[:20],
      }


  def cull_orphan_rows(conn: sqlite3.Connection, orphans: list[dict]) -> int:
      """Delete DB rows whose JPG is missing. Returns count deleted."""
      if not orphans:
          return 0
      ids = [o["id"] for o in orphans]
      placeholders = ",".join("?" * len(ids))
      cur = conn.execute(f"DELETE FROM classifications WHERE id IN ({placeholders})", ids)
      conn.commit()
      return cur.rowcount


  def main():
      ap = argparse.ArgumentParser()
      ap.add_argument("--cull", action="store_true",
                      help="delete orphan DB rows (default: report only)")
      ap.add_argument("--json", action="store_true",
                      help="emit machine-readable JSON to stdout")
      args = ap.parse_args()

      logging.basicConfig(
          filename=LOG, level=logging.INFO,
          format="%(asctime)s %(levelname)s %(message)s",
      )
      log = logging.getLogger(__name__)

      conn = sqlite3.connect(str(DB))
      conn.row_factory = sqlite3.Row
      report = audit(conn)

      log.info(
          "audit: rows=%d disk=%d orphan_rows=%d orphan_files=%d",
          report["total_rows_checked"], report["disk_files_found"],
          len(report["orphan_rows"]), report["orphan_files_count"],
      )
      # Detail log for each orphan — so the next occurrence is diagnosable
      for o in report["orphan_rows"]:
          log.info("orphan_row id=%d file=%s species=%s ts=%s action=%s",
                   o["id"], o["file"], o["species"], o["source_timestamp"], o["action"])

      culled = 0
      if args.cull and report["orphan_rows"]:
          culled = cull_orphan_rows(conn, report["orphan_rows"])
          log.warning("culled %d orphan rows", culled)
      report["culled"] = culled
      conn.close()

      if args.json:
          print(json.dumps(report, indent=2, default=str))
      else:
          print(f"rows checked:       {report['total_rows_checked']}")
          print(f"disk files found:   {report['disk_files_found']}")
          print(f"orphan rows:        {len(report['orphan_rows'])}")
          print(f"orphan files:       {report['orphan_files_count']} (not deleted; sample below)")
          for f in report["orphan_files_sample"]:
              print(f"  {f}")
          if args.cull:
              print(f"CULLED: {culled} DB rows")
          else:
              print("(dry-run; pass --cull to delete orphan rows)")


  if __name__ == "__main__":
      sys.exit(main())
  ```

- [ ] **Step 1.2: Run dry-run**

  ```bash
  cd ~/bird-classifier
  python3 tools/audit_data_integrity.py
  ```

  Evidence gate: David reviews the orphan-row count. It should match the "pre-orphan rows" count from the 0b dry-run to within ± a few. If they diverge wildly, the hypothesis (that 0b's pre-orphans are the real orphans) is wrong — investigate before Task 2.

---

### Task 2: Execute cull

- [ ] **Step 2.1: Run with --cull**

  ```bash
  cd ~/bird-classifier
  python3 tools/audit_data_integrity.py --cull
  ```

- [ ] **Step 2.2: Re-run dry-run to confirm zero orphan rows after cull**

  ```bash
  python3 tools/audit_data_integrity.py
  ```
  Expected: `orphan rows: 0`.

  Evidence gate: second run shows 0. If not, repeat on any rows that came back.

---

### Task 3: Install hourly launchd agent

**Files:**
- Create: `~/Library/LaunchAgents/com.vives.bird-integrity-audit.plist`

- [ ] **Step 3.1: Write the plist**

  ```xml
  <?xml version="1.0" encoding="UTF-8"?>
  <!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
  <plist version="1.0">
  <dict>
    <key>Label</key>
    <string>com.vives.bird-integrity-audit</string>
    <key>ProgramArguments</key>
    <array>
      <string>/Users/vives/bird-classifier/venv/bin/python3</string>
      <string>/Users/vives/bird-classifier/tools/audit_data_integrity.py</string>
      <string>--cull</string>
    </array>
    <key>StartInterval</key>
    <integer>3600</integer>
    <key>RunAtLoad</key>
    <true/>
    <key>StandardOutPath</key>
    <string>/Users/vives/Library/Logs/bird-audit.log</string>
    <key>StandardErrorPath</key>
    <string>/Users/vives/Library/Logs/bird-audit.log</string>
    <key>WorkingDirectory</key>
    <string>/Users/vives/bird-classifier</string>
  </dict>
  </plist>
  ```

- [ ] **Step 3.2: Load it**

  ```bash
  launchctl load ~/Library/LaunchAgents/com.vives.bird-integrity-audit.plist
  sleep 5
  tail -20 ~/Library/Logs/bird-audit.log
  ```

  Expected: first log entry shows `rows=... orphan_rows=0`.

- [ ] **Step 3.3: Verify interval is active**

  ```bash
  launchctl list | grep bird-integrity
  ```
  Expected: PID + exit-code-0 + label.

---

### Task 4: Watch for the bug to return (add a dashboard signal)

- [ ] **Step 4.1: Add an audit snapshot to /api/system-health**

  Modify `~/bird-classifier/dashboard/api.py` — wherever the system-health endpoint is defined (grep for `system-health` or `system_health`) — add:

  ```python
  def _last_audit_orphan_count() -> int | None:
      """Return the orphan_row count from the last integrity-audit log line,
      or None if no recent log exists."""
      import re
      LOG = Path.home() / "bird-classifier" / "tools" / "audit_data_integrity.log"
      if not LOG.exists():
          return None
      try:
          # Grab the last "audit:" line
          tail = LOG.read_text().splitlines()[-50:]
          for line in reversed(tail):
              m = re.search(r"orphan_rows=(\d+)", line)
              if m:
                  return int(m.group(1))
      except Exception:
          return None
      return None
  ```

  And include it in the system-health response:
  ```python
  "integrity": {
      "orphan_rows_last_audit": _last_audit_orphan_count(),
  },
  ```

- [ ] **Step 4.2: Surface it in the health UI dropdown**

  In `dashboard/index.html`, find the section that renders `/api/system-health` (search for `system-health` or `api/system-health`). Add a row:

  ```
  Orphan rows: {integrity.orphan_rows_last_audit}
  ```
  — red if >0, green if 0, grey if null.

- [ ] **Step 4.3: Verify it shows 0 after cull**

  David clicks the system-health dots, sees "Orphan rows: 0."

  Evidence gate: David confirms visible + green.

- [ ] **Step 4.4: Commit**

  ```bash
  cd ~/bird-classifier
  git add tools/audit_data_integrity.py dashboard/api.py dashboard/index.html
  git commit -m "$(cat <<'EOF'
  Add data-integrity audit: cull orphan rows, detect them on every hour

  tools/audit_data_integrity.py verifies every classifications.db row has a
  JPG on disk. --cull deletes orphan rows. Hourly launchd agent runs it and
  logs every orphan before culling, so the next time the orphan bug bites
  we catch the row + timestamp + species → traceable to pipeline state.

  Orphan count is surfaced in /api/system-health → dashboard health dropdown.
  When it climbs, the log line tells us exactly which row + when → root-cause
  evidence, not just a cleanup.

  Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
  EOF
  )"
  ```

---

## Self-review notes

- **Spec coverage:** cull (Task 1-2), instrumentation (Task 3), dashboard signal (Task 4). Every piece David asked for.
- **Placeholder scan:** Task 4.1/4.2 reference "find the system-health endpoint" — the grep command is provided, but I'm not hard-coding lines because I haven't verified them. Acceptable ambiguity for this size of change.
- **Safety:** dry-run default; reversal is trivial (CULLED DB rows have no file anyway, so nothing to restore).
- **Root-cause hunt:** the auditor doesn't *fix* the orphan bug — it *detects* it and captures evidence. That's intentional. Fixing the bug needs the evidence first.