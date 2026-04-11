#!/bin/bash
# Temporarily stop/start the production v2 LaunchAgent so v3 can use Coral in tests.
# Coral USB can only host one process at a time.
set -e
ACTION="${1:-}"
PLIST="$HOME/Library/LaunchAgents/com.vives.bird-pipeline.plist"
case "$ACTION" in
  stop)
    if [ -f "$PLIST" ]; then
      launchctl unload "$PLIST" 2>/dev/null || true
      echo "Stopped com.vives.bird-pipeline"
    else
      echo "Plist not found: $PLIST"
      exit 1
    fi
    ;;
  start)
    if [ -f "$PLIST" ]; then
      launchctl load "$PLIST" 2>/dev/null || true
      echo "Started com.vives.bird-pipeline"
    else
      echo "Plist not found: $PLIST"
      exit 1
    fi
    ;;
  *)
    echo "usage: $0 {stop|start}"
    exit 1
    ;;
esac
