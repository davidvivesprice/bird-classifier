#!/usr/bin/env bash
# sim_set.sh — load one or more MP4 clips into the simulated feeder camera.
#
# Clips are scaled to 640x360 (the live substream resolution the detector sees)
# and concatenated in order, so you can chain "one bird after another" for a
# dense test reel. The result is served as a real-time loop on the go2rtc
# `feeder-demo` stream — the same path the real UniFi camera takes — so the
# pipeline and dashboard treat it exactly like a live camera.
#
# Usage (on the Pi):
#   tools/sim_set.sh clipA.mp4 [clipB.mp4 ...]
# Then turn on sim mode with tools/sim_mode.sh on, and toggle "demo" in the
# dashboard to watch it with the overlay.
set -euo pipefail

SIM_DIR=/home/vives/sim
OUT="$SIM_DIR/current.mp4"
TMP="$SIM_DIR/.current.tmp.mp4"
mkdir -p "$SIM_DIR"

if [ "$#" -lt 1 ]; then
  echo "usage: $0 clip1.mp4 [clip2.mp4 ...]" >&2
  exit 2
fi
for f in "$@"; do [ -f "$f" ] || { echo "no such clip: $f" >&2; exit 2; }; done

echo "building sim reel from $# clip(s) -> 640x360 @30fps ..."
if [ "$#" -eq 1 ]; then
  /usr/bin/ffmpeg -y -hide_banner -loglevel error -i "$1" \
    -vf "scale=640:360:force_original_aspect_ratio=decrease,pad=640:360:(ow-iw)/2:(oh-ih)/2,fps=30" \
    -c:v libx264 -preset veryfast -crf 23 -pix_fmt yuv420p -an "$TMP"
else
  # scale+pad each input to a uniform 640x360@30 then concat
  inputs=(); filters=""; i=0
  for f in "$@"; do
    inputs+=( -i "$f" )
    filters+="[$i:v]scale=640:360:force_original_aspect_ratio=decrease,pad=640:360:(ow-iw)/2:(oh-ih)/2,fps=30,setsar=1[v$i];"
    i=$((i+1))
  done
  maps=""; for ((j=0;j<i;j++)); do maps+="[v$j]"; done
  /usr/bin/ffmpeg -y -hide_banner -loglevel error "${inputs[@]}" \
    -filter_complex "${filters}${maps}concat=n=$i:v=1:a=0[out]" -map "[out]" \
    -c:v libx264 -preset veryfast -crf 23 -pix_fmt yuv420p -an "$TMP"
fi
mv -f "$TMP" "$OUT"

DUR=$(/usr/bin/ffprobe -v error -show_entries format=duration -of csv=p=0 "$OUT" 2>/dev/null || echo "?")
echo "sim reel ready: $OUT (${DUR}s)"

# go2rtc's exec: source holds the file open; restart to pick up the new reel.
systemctl --user restart go2rtc.service
sleep 5
echo "go2rtc restarted; feeder-demo now loops the new reel."
echo "next: tools/sim_mode.sh on   (then toggle 'demo' in the dashboard to watch)"
