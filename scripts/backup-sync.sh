#!/bin/bash
# REPO COPY (added 2026-07-17): canonical installed copy is /Users/vives/Backups/backup-sync.sh,
# referenced by launchd. This copy exists so the breaker/monitor/backup
# logic is versioned; keep both in sync when editing (cp to /Users/vives/Backups/backup-sync.sh).
# ── iMac Bird Observatory Backup Script ──
# Mirrors all critical data into ~/Backups/ for Syncthing to sync to VivesSyn NAS.
# Run manually or via LaunchAgent (com.vives.backup-sync).
#
# What gets backed up:
#   bird-classifier/  — code, dashboard, models, configs (also on GitHub)
#   bird-snapshots/   — classified images, JSONL logs, annotations (NOT on GitHub)
#   LaunchAgents/     — service plists for auto-start
#   claude-config/    — Claude Code project memory
#
# Syncthing then replicates ~/Backups/ → VivesSyn:/volume1/backups/

set -euo pipefail

SRC_CLASSIFIER="/Users/vives/bird-classifier"
SRC_SNAPSHOTS="/Users/vives/bird-snapshots"
SRC_LAUNCHAGENTS="$HOME/Library/LaunchAgents"
SRC_CLAUDE="$HOME/.claude/projects/-Users-vives-Scans/memory"

DEST="/Users/vives/Backups"

LOG="$DEST/.backup.log"

echo "$(date '+%Y-%m-%d %H:%M:%S') Backup started" >> "$LOG"

# ── bird-classifier (code + dashboard + models) ──
rsync -a --delete \
  --exclude='venv/' \
  --exclude='__pycache__/' \
  --exclude='*.pyc' \
  --exclude='.git/' \
  "$SRC_CLASSIFIER/" "$DEST/bird-classifier/"

# ── bird-snapshots (classification data — the irreplaceable stuff) ──
# hls/ excluded: HLS .ts segments are transient (recorder deletes old ones every
# ~30s via -hls_flags delete_segments), so rsync races them and exits 24.
# .rsync-partial excluded for the same reason.
rsync -a --delete \
  --exclude='__pycache__/' \
  --exclude='*.pyc' \
  --exclude='.DS_Store' \
  --exclude='hls/' \
  --exclude='.rsync-partial/' \
  "$SRC_SNAPSHOTS/" "$DEST/bird-snapshots/" || {
    rc=$?
    # 24 = vanished source files (benign race with HLS/ffmpeg). Keep going.
    [ "$rc" -eq 24 ] || exit "$rc"
  }

# ── LaunchAgents (service configs) ──
# SECURITY: refuse to copy any plist that embeds credentials. Creds should
# live in ~/.bird-observatory-env and be loaded via scripts/run-with-env.sh.
# If this tripwire fires, FIX the plist — don't weaken the check.
mkdir -p "$DEST/LaunchAgents"
copy_plist_safe() {
  local src="$1" dest="$2"
  [ -f "$src" ] || return 0
  if grep -qE 'PROTECT_PASSWORD|UNIFI_API_KEY|PROTECT_USERNAME' "$src"; then
    echo "$(date '+%Y-%m-%d %H:%M:%S') SKIP (has creds): $(basename "$src")" >> "$LOG"
    return 0
  fi
  cp -f "$src" "$dest"
}
for p in "$SRC_LAUNCHAGENTS"/com.vives.bird-*.plist \
         "$SRC_LAUNCHAGENTS"/syncthing.plist \
         "$SRC_LAUNCHAGENTS"/com.vives.backup-sync.plist; do
  copy_plist_safe "$p" "$DEST/LaunchAgents/"
done

# ── Claude project memory ──
mkdir -p "$DEST/claude-config"
cp -f "$SRC_CLAUDE/MEMORY.md" "$DEST/claude-config/" 2>/dev/null || true

echo "$(date '+%Y-%m-%d %H:%M:%S') Backup completed" >> "$LOG"

# Keep log from growing forever
tail -100 "$LOG" > "$LOG.tmp" && mv "$LOG.tmp" "$LOG"
