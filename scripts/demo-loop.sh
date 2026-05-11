#!/usr/bin/env bash
# bird-demo-loop helper: starts mediamtx on :8654 + ffmpeg loops demo video into it.
# Invoked by ~/.config/systemd/user/bird-demo-loop.service.

set -euo pipefail

# Pre-encoded 640×360 H.264 (2s GOP) — matches the camera's native substream
# resolution. FrameCapture decodes this directly without resize. Source was
# the 1080p may10_demo_normalized.mp4; rescaled once at:
#   ffmpeg -i may10_demo_normalized.mp4 -vf scale=640:360 -c:v libx264 \
#          -preset veryfast -crf 23 -g 60 -keyint_min 60 -sc_threshold 0 \
#          -profile:v main -pix_fmt yuv420p -an may10_demo_640x360.mp4
VIDEO="/home/vives/bird-snapshots/demo/may10_demo_640x360.mp4"
PORT=8654
STREAM=feeder-main

if [[ ! -f "$VIDEO" ]]; then
    echo "demo video not found: $VIDEO" >&2
    exit 1
fi

# Minimal mediamtx config in /tmp (per-process, gets cleaned on stop).
TMPCONF=$(mktemp -d)
trap 'rm -rf "$TMPCONF"; pkill -P $$ -f mediamtx || true' EXIT

cat > "$TMPCONF/mediamtx.yml" <<EOF
rtspAddress: :$PORT
rtmpDisable: true
hlsDisable: true
webrtcDisable: true
paths:
  all:
    source: publisher
EOF

# Start mediamtx in the background, give it a second to listen.
~/.local/bin/mediamtx "$TMPCONF/mediamtx.yml" &
MTX_PID=$!
sleep 1

# Loop the video into it. ffmpeg in the foreground so systemd watches IT.
exec ffmpeg -hide_banner -loglevel warning \
    -re -stream_loop -1 -i "$VIDEO" \
    -c copy -f rtsp rtsp://localhost:$PORT/$STREAM
