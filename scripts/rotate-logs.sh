#!/bin/bash
# rotate-logs.sh — user-space log rotation for the iMac bird observatory.
#
# Why not newsyslog: launchd holds O_APPEND fds on these logs, so classic
# rename-rotation would keep writes flowing into the *rotated* file while the
# fresh one stays empty until the service restarts. gzip-copy + truncate is
# the safe pattern: O_APPEND fds always write at the (new) EOF, so writers
# continue seamlessly into the truncated file. (A few lines written between
# the copy and the truncate can be lost — standard copytruncate tradeoff.)
#
# Runs daily via com.vives.bird-log-rotate.plist; safe to run by hand.
set -u
THRESHOLD=$((10*1024*1024))   # rotate anything >= 10 MB
KEEP=5                        # compressed generations to keep per log
TS=$(date +%Y%m%d-%H%M%S)

rotate() {
  local f="$1" size
  [ -f "$f" ] || return 0
  size=$(stat -f%z "$f" 2>/dev/null || echo 0)
  [ "$size" -ge "$THRESHOLD" ] || return 0
  if gzip -c "$f" > "$f.$TS.gz"; then
    : > "$f"
    echo "$(date '+%F %T') rotated $f ($size bytes) -> $f.$TS.gz"
    # prune archives beyond KEEP (newest kept)
    ls -t "$f".*.gz 2>/dev/null | tail -n +$((KEEP+1)) | while read -r old; do
      rm -f "$old" && echo "$(date '+%F %T') pruned $old"
    done
  else
    echo "$(date '+%F %T') ERROR: gzip failed for $f — left untouched"
    rm -f "$f.$TS.gz"
  fi
}

for f in /Users/vives/bird-snapshots/logs/*.log /Users/vives/Library/Logs/bird-audit.log; do
  rotate "$f"
done
