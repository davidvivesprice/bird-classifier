#!/bin/bash
# Sync new bird snapshots from VivesSyn to local incoming directory.
# Uses SSH+scp instead of rsync (macOS openrsync has SSH transport bugs).
# Designed to run via LaunchAgent every 60 seconds.
# Locking: uses lockf (macOS) to prevent concurrent runs.
set -eu

REMOTE_HOST="vives@192.168.5.92"
REMOTE_PORT=2000
REMOTE_DIR="/volume1/docker/bird-snapshots/captures"
LOCAL_DIR="/Users/vives/bird-snapshots/incoming"
SSH_KEY="/Users/vives/.ssh/id_ed25519"
SSH_OPTS="-p ${REMOTE_PORT} -i ${SSH_KEY} -o ConnectTimeout=5 -o BatchMode=yes"

# List remote files, compare with local, download new ones
remote_files=$(ssh ${SSH_OPTS} "${REMOTE_HOST}" "ls ${REMOTE_DIR}/" 2>/dev/null) || exit 0

new_count=0
for f in ${remote_files}; do
    # Skip non-jpg files
    case "${f}" in *.jpg) ;; *) continue ;; esac

    # Skip if we already have it (in incoming, classified, or failed)
    if [ -f "${LOCAL_DIR}/${f}" ] || \
       [ -f "/Users/vives/bird-snapshots/classified/${f}" ] || \
       [ -f "/Users/vives/bird-snapshots/failed/${f}" ]; then
        continue
    fi

    # Download new file (atomic: write .tmp, then rename)
    scp -P ${REMOTE_PORT} -i ${SSH_KEY} -o ConnectTimeout=5 -o BatchMode=yes \
        "${REMOTE_HOST}:${REMOTE_DIR}/${f}" "${LOCAL_DIR}/${f}.tmp" 2>/dev/null && \
        chmod u+rw "${LOCAL_DIR}/${f}.tmp" && \
        mv "${LOCAL_DIR}/${f}.tmp" "${LOCAL_DIR}/${f}" && \
        new_count=$((new_count + 1))
done

if [ "${new_count}" -gt 0 ]; then
    echo "$(date '+%Y-%m-%d %H:%M:%S') Synced ${new_count} new snapshot(s)"
fi
