#!/bin/bash
# Quick health check — run this first to see what's working and what's not
# Usage: bash ~/bird-classifier/check_health.sh

echo "═══════════════════════════════════════════"
echo "  Bird Observatory Health Check"
echo "  $(date)"
echo "═══════════════════════════════════════════"
echo ""

# Services
echo "▸ SERVICES"
for svc in bird-dashboard bird-livedetect bird-classifier bird-enhanced-audio bird-audio bird-sync bird-capture; do
  pid=$(launchctl list "com.vives.$svc" 2>/dev/null | grep '"PID"' | awk '{print $3}' | tr -d ';')
  if [ -n "$pid" ] && [ "$pid" != "0" ]; then
    cpu=$(ps -p "$pid" -o %cpu= 2>/dev/null | tr -d ' ')
    mem=$(ps -p "$pid" -o %mem= 2>/dev/null | tr -d ' ')
    printf "  ✅ %-25s PID %-6s CPU %s%%  MEM %s%%\n" "$svc" "$pid" "$cpu" "$mem"
  else
    printf "  ❌ %-25s NOT RUNNING\n" "$svc"
  fi
done
echo ""

# API
echo "▸ API HEALTH"
health=$(curl -s -m 5 http://localhost:8099/api/system-health 2>/dev/null)
if [ -n "$health" ]; then
  echo "$health" | python3 -c "
import sys, json
try:
    d = json.load(sys.stdin)
    for svc, info in d['services'].items():
        s = info.get('status','?')
        det = info.get('detail','')
        icon = '✅' if s == 'ok' else '⚠️' if s == 'warn' else '❌'
        print(f'  {icon} {svc}: {det}')
    print(f'  Disk: {d.get(\"disk_free_gb\",\"?\"):.0f} GB free / {d.get(\"disk_total_gb\",\"?\"):.0f} GB total')
except: print('  ⚠️  Could not parse health response')
" 2>/dev/null
else
  echo "  ❌ API not responding (may still be loading JSONL — takes ~40s)"
fi
echo ""

# Backlog
echo "▸ CLASSIFIER BACKLOG"
incoming=$(ls /Users/vives/bird-snapshots/incoming/ 2>/dev/null | wc -l | tr -d ' ')
echo "  $incoming files in incoming/"
echo ""

# RTSP
echo "▸ AUDIO RTSP STATUS"
last_audio_err=$(tail -1 /Users/vives/bird-snapshots/logs/audio-analyzer-stderr.log 2>/dev/null)
if echo "$last_audio_err" | grep -q "Invalid data"; then
  echo "  ❌ Audio analyzer: RTSP token expired or invalid"
elif echo "$last_audio_err" | grep -q "Reconnecting"; then
  echo "  ⚠️  Audio analyzer: reconnecting"
else
  echo "  ✅ Audio analyzer: connected"
fi

last_enhanced_err=$(tail -1 /Users/vives/bird-snapshots/logs/enhanced-audio-stderr.log 2>/dev/null)
if echo "$last_enhanced_err" | grep -q "Invalid data"; then
  echo "  ❌ Enhanced audio: RTSP token expired or invalid"
elif echo "$last_enhanced_err" | grep -q "Reconnecting"; then
  echo "  ⚠️  Enhanced audio: reconnecting"
else
  echo "  ✅ Enhanced audio: connected"
fi
echo ""

# NAS
echo "▸ NAS CONNECTIVITY"
ssh -p 2000 -i ~/.ssh/id_ed25519 -o ConnectTimeout=3 -o BatchMode=yes vives@192.168.5.92 "echo OK" 2>/dev/null
if [ $? -eq 0 ]; then
  echo "  ✅ SSH to NAS working"
  containers=$(ssh -p 2000 -i ~/.ssh/id_ed25519 vives@192.168.5.92 "sudo /usr/local/bin/docker ps --format '{{.Names}}:{{.Status}}'" 2>/dev/null | grep -c "Up")
  echo "  ✅ $containers Docker containers running"
else
  echo "  ❌ Cannot SSH to NAS (192.168.5.92:2000)"
fi
echo ""

# JSONL size
echo "▸ DATA"
jsonl_size=$(du -sh /Users/vives/bird-snapshots/logs/classifications.jsonl 2>/dev/null | awk '{print $1}')
jsonl_lines=$(wc -l < /Users/vives/bird-snapshots/logs/classifications.jsonl 2>/dev/null | tr -d ' ')
echo "  JSONL: $jsonl_size / $jsonl_lines entries"
echo ""

echo "═══════════════════════════════════════════"
echo "  Docs: ~/docs/bird-observatory/migration/HANDOFF.md"
echo "═══════════════════════════════════════════"
