#!/bin/bash
# Bird Observatory — System Verification
# Run before deploying changes or to check system health.
# Exit 0 = all good, Exit 1 = problems found.
set -u

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[0;33m'
NC='\033[0m'
FAIL=0

pass() { echo -e "  ${GREEN}✓${NC} $1"; }
warn() { echo -e "  ${YELLOW}!${NC} $1"; }
fail() { echo -e "  ${RED}✗${NC} $1"; FAIL=1; }

echo "Bird Observatory — System Verification"
echo "========================================"

# 1. Tests
echo ""
echo "Tests:"
TEST_OUT=$(cd ~/bird-classifier && ~/bird-classifier/venv/bin/python -m pytest tests/ -q --tb=no 2>&1 | tail -1)
if echo "$TEST_OUT" | grep -q "passed"; then
    pass "$TEST_OUT"
else
    fail "Tests: $TEST_OUT"
fi

# 2. Syntax check — all service entry points import cleanly
echo ""
echo "Imports:"
for mod in audio_analyzer classify live_detector capture_snapshots enhanced_audio_stream; do
    if PYTHONPATH=~/bird-classifier/venv-coral/lib/python3.9/site-packages /usr/bin/python3 -c "import $mod" 2>/dev/null; then
        pass "$mod"
    else
        fail "$mod failed to import"
    fi
done

# 3. No stale references
echo ""
echo "Stale references:"
STALE=$(grep -rn "_get_rtsp_url\|DetectionAccumulator\|DEEP_DETECTION" ~/bird-classifier/*.py 2>/dev/null | grep -v vendor | grep -v __pycache__ | grep -v test_)
if [ -z "$STALE" ]; then
    pass "No stale function references"
else
    fail "Stale references found: $STALE"
fi

# 4. API prefix consistency
echo ""
echo "API prefix check:"
BAD_PREFIX=$(grep -n "fetch('/api/" ~/bird-classifier/dashboard/index.html 2>/dev/null | grep -v "bird-api")
if [ -z "$BAD_PREFIX" ]; then
    pass "All fetch calls use /bird-api/ prefix"
else
    fail "Bare /api/ fetch calls found (won't work through NAS proxy)"
fi

# 5. No hardcoded credentials
echo ""
echo "Credentials check:"
CREDS=$(grep -rn "9X1Ua2_\|pW3nRj5vKz" ~/bird-classifier/*.py ~/bird-classifier/dashboard/*.py 2>/dev/null | grep -v vendor | grep -v __pycache__)
if [ -z "$CREDS" ]; then
    pass "No hardcoded credentials in source"
else
    fail "Hardcoded credentials found!"
fi

# 6. Services running
echo ""
echo "Services:"
for svc in bird-audio bird-classifier bird-dashboard bird-capture bird-livedetect bird-enhanced-audio; do
    PID=$(launchctl list 2>/dev/null | grep "com.vives.$svc" | awk '{print $1}')
    if [ "$PID" != "-" ] && [ -n "$PID" ]; then
        pass "$svc (PID $PID)"
    else
        warn "$svc not running"
    fi
done

# 7. Health files fresh
echo ""
echo "Audio health:"
python3 -c "
import json, glob
from datetime import datetime
ok = True
for f in sorted(glob.glob('/tmp/audio-stream-health-*.json')):
    d = json.load(open(f))
    updated = datetime.fromisoformat(d['updated'])
    age = (datetime.now() - updated).total_seconds()
    if age > 300:
        print(f'  STALE: {d[\"service\"]} ({int(age)}s ago)')
        ok = False
    else:
        print(f'  FRESH: {d[\"service\"]}')
if not ok:
    exit(1)
" 2>/dev/null || warn "Health files stale or missing"

# 8. Classifier queue
echo ""
echo "Classifier queue:"
QUEUE=$(ls ~/bird-snapshots/incoming/*.jpg 2>/dev/null | wc -l | tr -d ' ')
if [ "$QUEUE" -lt 500 ]; then
    pass "$QUEUE files (healthy)"
elif [ "$QUEUE" -lt 2000 ]; then
    warn "$QUEUE files (growing)"
else
    fail "$QUEUE files (backlogged!)"
fi

# 9. Git status
echo ""
echo "Git:"
DIRTY=$(cd ~/bird-classifier && git status --porcelain 2>/dev/null | grep -v "^??" | head -3)
if [ -z "$DIRTY" ]; then
    pass "Working tree clean"
else
    warn "Uncommitted changes"
fi

# Summary
echo ""
echo "========================================"
if [ "$FAIL" -eq 0 ]; then
    echo -e "${GREEN}All checks passed${NC}"
    exit 0
else
    echo -e "${RED}Some checks failed${NC}"
    exit 1
fi
