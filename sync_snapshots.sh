#!/bin/bash
# Sync new bird snapshots from VivesSyn to local incoming directory.
# Uses a single SSH connection with tar pipe for batch transfer (fast).
# Designed to run via LaunchAgent every 60 seconds.
# Locking: uses lockf (macOS) to prevent concurrent runs.
# NOTE: Must be bash 3.2 compatible (macOS default — no associative arrays).
set -eu

REMOTE_HOST="vives@192.168.5.92"
REMOTE_PORT=2000
REMOTE_DIR="/volume1/docker/bird-snapshots/captures"
LOCAL_DIR="/Users/vives/bird-snapshots/incoming"
SSH_KEY="/Users/vives/.ssh/id_ed25519"
SSH_OPTS="-p ${REMOTE_PORT} -i ${SSH_KEY} -o ConnectTimeout=5 -o BatchMode=yes"
SNAPSHOTS_DIR="/Users/vives/bird-snapshots"

mkdir -p "${LOCAL_DIR}"

# Build sorted list of already-processed filenames (fast: only basenames)
processed_list=$(mktemp /tmp/bird-sync-processed.XXXXXX)
needed_list=$(mktemp /tmp/bird-sync-needed.XXXXXX)
trap "rm -f ${processed_list} ${needed_list} ${needed_list}.remote" EXIT

{
    ls "${SNAPSHOTS_DIR}"/classified/*/*.jpg 2>/dev/null || true
    ls "${SNAPSHOTS_DIR}"/skipped/*.jpg 2>/dev/null || true
    ls "${SNAPSHOTS_DIR}"/failed/*.jpg 2>/dev/null || true
    ls "${LOCAL_DIR}"/*.jpg 2>/dev/null || true
} | while IFS= read -r p; do
    basename "$p"
done | sort -u > "${processed_list}"

# Only list recent remote files (last 3 hours) — avoids scanning thousands of old files
ssh ${SSH_OPTS} "${REMOTE_HOST}" \
    "find ${REMOTE_DIR} -name '*.jpg' -mmin -180 -printf '%f\n' 2>/dev/null" \
    2>/dev/null | sort -u > "${needed_list}.remote" || exit 0

if [ ! -s "${needed_list}.remote" ]; then
    exit 0
fi

# Diff against processed list to find files we need
comm -23 "${needed_list}.remote" "${processed_list}" > "${needed_list}"

new_count=$(wc -l < "${needed_list}" | tr -d ' ')

if [ "${new_count}" -eq 0 ]; then
    exit 0
fi

# Batch download via tar over a SINGLE SSH connection.
# Use -T to read file list from a remote temp file (avoids arg length limits).
# Upload the needed list, tar from it, download in one shot.
ssh ${SSH_OPTS} "${REMOTE_HOST}" \
    "cd ${REMOTE_DIR} && tar cf - -T -" \
    < "${needed_list}" \
    | tar xf - -C "${LOCAL_DIR}" 2>/dev/null

# Count how many actually arrived
actual_count=0
while IFS= read -r f; do
    if [ -f "${LOCAL_DIR}/${f}" ]; then
        actual_count=$((actual_count + 1))
    fi
done < "${needed_list}"

if [ "${actual_count}" -gt 0 ]; then
    echo "$(date '+%Y-%m-%d %H:%M:%S') Synced ${actual_count} new snapshot(s)"
fi
