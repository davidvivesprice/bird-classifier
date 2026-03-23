#!/bin/bash
# Sync RTSP URLs from NAS after the nightly token refresh.
# Can be run by the 3:10 AM LaunchAgent or on-demand by RTSPStreamManager.
#
# Features:
#   - 3 SCP attempts with backoff (immediate, 5s, 10s)
#   - SSH cat fallback if all SCP attempts fail
#   - Lockfile prevents concurrent runs
#   - JSON validation before replacing
#
# Exit codes:
#   0 = success
#   1 = all transfer methods failed
set -eu

REMOTE_HOST="vives@192.168.5.92"
REMOTE_PORT=2000
SSH_KEY="/Users/vives/.ssh/id_ed25519"
SSH_OPTS="-i ${SSH_KEY} -o ConnectTimeout=10 -o BatchMode=yes"
REMOTE_FILE="/volume1/docker/birds-hls/rtsp_urls.json"
LOCAL_FILE="/Users/vives/bird-classifier/rtsp_urls.json"
LOCKFILE="/tmp/sync-rtsp-urls.lock"

log() { echo "$(date '+%Y-%m-%d %H:%M:%S') $*"; }

# ── Lockfile ──
if [ -f "$LOCKFILE" ]; then
    LOCK_PID=$(cat "$LOCKFILE" 2>/dev/null || true)
    if [ -n "$LOCK_PID" ] && kill -0 "$LOCK_PID" 2>/dev/null; then
        log "Another sync is running (PID $LOCK_PID), exiting"
        exit 0
    fi
    log "Stale lockfile found, removing"
    rm -f "$LOCKFILE"
fi
echo $$ > "$LOCKFILE"
trap 'rm -f "$LOCKFILE"' EXIT

# ── SCP with retry ──
SCP_DELAYS=(0 5 10)
FETCHED=false

for i in "${!SCP_DELAYS[@]}"; do
    delay=${SCP_DELAYS[$i]}
    attempt=$((i + 1))
    [ "$delay" -gt 0 ] && sleep "$delay"
    log "SCP attempt $attempt/3..."
    if scp -P ${REMOTE_PORT} ${SSH_OPTS} "${REMOTE_HOST}:${REMOTE_FILE}" "${LOCAL_FILE}.tmp" 2>/dev/null; then
        FETCHED=true
        log "SCP succeeded on attempt $attempt"
        break
    fi
    log "SCP attempt $attempt failed"
done

# ── SSH cat fallback ──
if [ "$FETCHED" = false ]; then
    log "All SCP attempts failed, trying SSH cat..."
    if ssh -p ${REMOTE_PORT} ${SSH_OPTS} "${REMOTE_HOST}" "cat ${REMOTE_FILE}" > "${LOCAL_FILE}.tmp" 2>/dev/null; then
        FETCHED=true
        log "SSH cat succeeded"
    else
        log "SSH cat also failed"
    fi
fi

if [ "$FETCHED" = false ]; then
    log "ERROR: All transfer methods failed"
    rm -f "${LOCAL_FILE}.tmp"
    exit 1
fi

# ── Validate JSON ──
if ! python3 -c "import json; json.load(open('${LOCAL_FILE}.tmp'))" 2>/dev/null; then
    log "ERROR: Invalid JSON in fetched file"
    rm -f "${LOCAL_FILE}.tmp"
    exit 1
fi

# ── Atomic replace ──
mv "${LOCAL_FILE}.tmp" "${LOCAL_FILE}"
log "Updated rtsp_urls.json"
