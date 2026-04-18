#!/usr/bin/env bash
# Phase 0: Pre-downgrade backup to the internal HDD.
# Copies every irreplaceable thing + every reproducible-but-annoying thing.
# Skips transient data (skipped/, hls/, incoming/) to keep backup tight.
#
# Target:  /Volumes/Internal/bird-observatory-backup-<timestamp>/
# Safe to re-run; each invocation creates a timestamped dir.

set -e
set -u
set -o pipefail

BACKUP_ROOT="/Volumes/Internal"
STAMP="$(date +%Y%m%d-%H%M%S)"
DEST="${BACKUP_ROOT}/bird-observatory-backup-${STAMP}"

if [ ! -d "$BACKUP_ROOT" ]; then
  echo "ERROR: ${BACKUP_ROOT} not mounted. Connect internal HDD first." >&2
  exit 1
fi

mkdir -p "${DEST}"
echo "=== Phase 0 backup → ${DEST} ==="
echo ""

# -----------------------------------------------------------------
# 1. Databases (SQLite — use .backup for consistent snapshots while live)
# -----------------------------------------------------------------
echo "[1/8] SQLite databases (consistent snapshots)..."
mkdir -p "${DEST}/databases"
for db in /Users/vives/bird-snapshots/logs/classifications.db \
          /Users/vives/bird-snapshots/logs/pipeline.db \
          /Users/vives/bird-snapshots/logs/birdnet_local.db \
          /Users/vives/bird-snapshots/logs/pipeline_v3_dev.db; do
  if [ -f "$db" ]; then
    name=$(basename "$db")
    echo "    $name"
    sqlite3 "$db" ".backup '${DEST}/databases/${name}'"
  fi
done
# Copy any additional .db files that exist but we didn't hardcode
for db in /Users/vives/bird-snapshots/logs/*.db; do
  name=$(basename "$db")
  if [ ! -f "${DEST}/databases/${name}" ]; then
    echo "    $name (cp — non-sqlite or tiny)"
    cp "$db" "${DEST}/databases/"
  fi
done

# Existing .bak files (e.g. the April 9 wipe backup)
cp /Users/vives/bird-snapshots/logs/*.bak-* "${DEST}/databases/" 2>/dev/null || true

# -----------------------------------------------------------------
# 2. Snapshot images (classified + annotated)
#    76G skipped/ deliberately excluded — no-bird frames, reproducible
# -----------------------------------------------------------------
echo ""
echo "[2/8] Classified images (~17G) + annotated (~27G)..."
mkdir -p "${DEST}/bird-snapshots"
/usr/local/bin/rsync -a --info=progress2 --human-readable \
    /Users/vives/bird-snapshots/classified \
    /Users/vives/bird-snapshots/annotated \
    /Users/vives/bird-snapshots/trash \
    /Users/vives/bird-snapshots/failed \
    /Users/vives/bird-snapshots/species_images \
    "${DEST}/bird-snapshots/"

# -----------------------------------------------------------------
# 3. Audio clips (BirdNET detections)
# -----------------------------------------------------------------
echo ""
echo "[3/8] BirdNET audio (~12G)..."
/usr/local/bin/rsync -a --info=progress2 --human-readable \
    /Users/vives/bird-snapshots/birdnet-audio \
    "${DEST}/bird-snapshots/"

# -----------------------------------------------------------------
# 4. The repo, minus venv + __pycache__ + the massive skipped frames
# -----------------------------------------------------------------
echo ""
echo "[4/8] bird-classifier repo (minus venv/caches)..."
mkdir -p "${DEST}/bird-classifier"
/usr/local/bin/rsync -a --info=progress2 --human-readable \
    --exclude='venv/' \
    --exclude='__pycache__/' \
    --exclude='.pytest_cache/' \
    --exclude='*.pyc' \
    --exclude='node_modules/' \
    /Users/vives/bird-classifier/ \
    "${DEST}/bird-classifier/"

# -----------------------------------------------------------------
# 5. Service configs — launchctl plists
# -----------------------------------------------------------------
echo ""
echo "[5/8] LaunchAgents..."
mkdir -p "${DEST}/LaunchAgents"
cp ~/Library/LaunchAgents/com.vives.*.plist "${DEST}/LaunchAgents/" 2>/dev/null || true
launchctl list | grep vives > "${DEST}/LaunchAgents/_running_snapshot.txt" || true

# -----------------------------------------------------------------
# 6. Homebrew + Python inventories (so we can recreate the environment)
# -----------------------------------------------------------------
echo ""
echo "[6/8] System inventories..."
mkdir -p "${DEST}/system"
brew list --formula > "${DEST}/system/brew-formula.txt" 2>/dev/null || true
brew list --cask    > "${DEST}/system/brew-cask.txt"    2>/dev/null || true
brew --version      > "${DEST}/system/brew-version.txt" 2>/dev/null || true
brew services list  > "${DEST}/system/brew-services.txt" 2>/dev/null || true
if [ -x /Users/vives/bird-classifier/venv/bin/pip ]; then
  /Users/vives/bird-classifier/venv/bin/pip freeze > "${DEST}/system/pip-freeze.txt" 2>/dev/null || true
fi
sw_vers > "${DEST}/system/sw_vers.txt"
system_profiler SPHardwareDataType SPSoftwareDataType > "${DEST}/system/system_profiler.txt" 2>/dev/null || true
diskutil list > "${DEST}/system/diskutil-list.txt"
crontab -l > "${DEST}/system/crontab.txt" 2>/dev/null || echo "(no crontab)" > "${DEST}/system/crontab.txt"

# Process list snapshot (helps remember what was running)
ps -ax -o pid,command > "${DEST}/system/processes.txt"

# Network config
networksetup -listallnetworkservices > "${DEST}/system/network-services.txt" 2>/dev/null || true

# -----------------------------------------------------------------
# 7. Cloudflared tunnel config (if present)
# -----------------------------------------------------------------
echo ""
echo "[7/8] Cloudflared tunnel + related configs..."
mkdir -p "${DEST}/cloudflared"
cp -r ~/.cloudflared "${DEST}/cloudflared/dot-cloudflared" 2>/dev/null || true
cp -r /usr/local/etc/cloudflared "${DEST}/cloudflared/etc-cloudflared" 2>/dev/null || true

# Any other /usr/local/etc configs we might care about (go2rtc yaml, etc.)
mkdir -p "${DEST}/usr-local-etc"
for cfg in /usr/local/etc/go2rtc.yaml /usr/local/etc/go2rtc /usr/local/etc/go2rtc.yml; do
  if [ -e "$cfg" ]; then
    cp -r "$cfg" "${DEST}/usr-local-etc/"
  fi
done

# -----------------------------------------------------------------
# 8. Claude memory + project notes (the forget-me-nots, handoffs, etc.)
# -----------------------------------------------------------------
echo ""
echo "[8/8] Claude memory files (forget-me-nots, handoffs)..."
mkdir -p "${DEST}/claude-memory"
if [ -d /Users/vives/.claude/projects/-Users-vives/memory ]; then
  cp -r /Users/vives/.claude/projects/-Users-vives/memory "${DEST}/claude-memory/"
fi

# -----------------------------------------------------------------
# Summary
# -----------------------------------------------------------------
echo ""
echo "=== Done ==="
du -sh "${DEST}"
echo ""
echo "Top-level contents:"
ls -la "${DEST}/"
echo ""
echo "Database snapshot sizes:"
ls -lh "${DEST}/databases/"
echo ""
echo "Backup location: ${DEST}"
echo ""
echo "Before proceeding to Phase 1, verify the backup by browsing:"
echo "    open '${DEST}'"
