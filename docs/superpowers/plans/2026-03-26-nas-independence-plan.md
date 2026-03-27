# NAS Independence Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Remove all NAS dependencies. go2rtc and RTSP refresh run on iMac. Clean up NAS crons and dead Docker containers.

**Architecture:** go2rtc runs locally on iMac with a LaunchAgent. A simplified `refresh_rtsp.py` fetches tokens directly from CloudKey and updates go2rtc.yaml + rtsp_urls.json. NAS bird crons and dead containers are cleaned up. The `sync_snapshots.sh` and `sync_rtsp_urls.sh` scripts are retired.

**Tech Stack:** go2rtc binary, Python 3, LaunchAgents, SSH to NAS for cleanup

**Spec:** `docs/superpowers/specs/2026-03-26-nas-independence-design.md`

---

## File Structure

| File | Action | Responsibility |
|------|--------|----------------|
| `go2rtc.yaml` | Create | go2rtc config — streams from CloudKey cameras |
| `refresh_rtsp.py` | Create | Fetch tokens from CloudKey, update rtsp_urls.json + go2rtc.yaml |
| `dashboard/api.py` | Modify | GO2RTC_HOST → 127.0.0.1 |
| `sync_rtsp_urls.sh` | Delete | Replaced by refresh_rtsp.py |
| `sync_snapshots.sh` | Delete | iMac captures directly, no NAS sync needed |
| `scripts/deploy.sh` | Modify | Remove NAS SCP |
| `scripts/verify.sh` | Modify | Remove NAS references |
| LaunchAgent `bird-go2rtc` | Create | go2rtc service |
| LaunchAgent `bird-rtsp-sync` | Modify | Run refresh_rtsp.py |
| LaunchAgent `bird-sync` | Unload | No more NAS sync |

---

### Task 1: go2rtc config and LaunchAgent

- [ ] **Step 1: Create go2rtc.yaml**

Read current RTSP URLs and write initial config:

```bash
python3 -c "
import json
urls = json.load(open('/Users/vives/bird-classifier/rtsp_urls.json'))
streams = urls.get('streams', {})
feeder = streams.get('birds', {})
ground = streams.get('ground', {})
feeder_url = feeder.get('high', feeder) if isinstance(feeder, dict) else feeder
ground_url = ground.get('high', ground) if isinstance(ground, dict) else ground
print(f'''streams:
  feeder-main:
    - {feeder_url}#tcp
  ground-main:
    - {ground_url}#tcp

api:
  listen: \":1984\"

log:
  level: info
''')
" > ~/bird-classifier/go2rtc.yaml
```

- [ ] **Step 2: Test go2rtc locally**

```bash
/usr/local/bin/go2rtc -config ~/bird-classifier/go2rtc.yaml &
sleep 3
curl -s http://localhost:1984/api/streams | python3 -m json.tool
kill %1
```

Expected: JSON listing feeder-main and ground-main streams.

- [ ] **Step 3: Create LaunchAgent**

Write `/Users/vives/Library/LaunchAgents/com.vives.bird-go2rtc.plist`:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.vives.bird-go2rtc</string>
    <key>ProgramArguments</key>
    <array>
        <string>/usr/local/bin/go2rtc</string>
        <string>-config</string>
        <string>/Users/vives/bird-classifier/go2rtc.yaml</string>
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string>/Users/vives/bird-snapshots/logs/go2rtc-stdout.log</string>
    <key>StandardErrorPath</key>
    <string>/Users/vives/bird-snapshots/logs/go2rtc-stderr.log</string>
    <key>WorkingDirectory</key>
    <string>/Users/vives/bird-classifier</string>
</dict>
</plist>
```

- [ ] **Step 4: Load and verify**

```bash
launchctl load ~/Library/LaunchAgents/com.vives.bird-go2rtc.plist
sleep 3
curl -s http://localhost:1984/api/streams
```

- [ ] **Step 5: Commit**

```bash
git add go2rtc.yaml
git commit -m "feat: go2rtc config and LaunchAgent on iMac"
```

---

### Task 2: refresh_rtsp.py

- [ ] **Step 1: Create refresh_rtsp.py**

Simplified version of the NAS script. Only fetches tokens from CloudKey, writes rtsp_urls.json and updates go2rtc.yaml.

Key points:
- Uses `UNIFI_PROTECT_API_KEY` env var (from LaunchAgent plist)
- Talks to CloudKey at `PROTECT_HOST` (default 192.168.4.9)
- Cameras: birds, ground, magnolia, newbackyard (same IDs as NAS script)
- Writes `rtsp_urls.json` (used by audio_analyzer, enhanced_audio_stream)
- Updates `go2rtc.yaml` (feeder-main and ground-main streams)
- Restarts go2rtc LaunchAgent if tokens changed
- No Frigate, BirdNET-Go, or NAS config updates

- [ ] **Step 2: Test manually**

```bash
UNIFI_PROTECT_API_KEY=9X1Ua2_GyZHsvW2jRTkO1-zcM-S2F_g- python3 refresh_rtsp.py
cat rtsp_urls.json | python3 -m json.tool | head -10
cat go2rtc.yaml
```

- [ ] **Step 3: Update bird-rtsp-sync LaunchAgent**

Modify `~/Library/LaunchAgents/com.vives.bird-rtsp-sync.plist` to run `refresh_rtsp.py` instead of `sync_rtsp_urls.sh`. Add the API key as an environment variable.

- [ ] **Step 4: Commit**

```bash
git add refresh_rtsp.py
git commit -m "feat: refresh_rtsp.py — fetch tokens directly from CloudKey"
```

---

### Task 3: Update WebSocket proxy to localhost

- [ ] **Step 1: Change GO2RTC_HOST**

In `dashboard/api.py`, change:
```python
GO2RTC_HOST = os.environ.get("GO2RTC_HOST", "100.73.76.98")
```
to:
```python
GO2RTC_HOST = os.environ.get("GO2RTC_HOST", "127.0.0.1")
```

- [ ] **Step 2: Test camera feed**

```bash
launchctl kickstart -k "gui/$(id -u)/com.vives.bird-dashboard"
sleep 3
~/bird-classifier/venv/bin/python -c "
import asyncio, websockets
async def test():
    async with websockets.connect('ws://localhost:8099/api/ws?src=feeder-main') as ws:
        await ws.send('{\"type\": \"mse\"}')
        msg = await asyncio.wait_for(ws.recv(), timeout=5)
        print(f'OK: {len(msg)} bytes')
asyncio.run(test())
"
```

- [ ] **Step 3: Commit**

```bash
git add dashboard/api.py
git commit -m "fix: go2rtc proxy → localhost (NAS no longer needed)"
```

---

### Task 4: Retire NAS sync scripts

- [ ] **Step 1: Unload bird-sync LaunchAgent**

```bash
launchctl unload ~/Library/LaunchAgents/com.vives.bird-sync.plist
```

- [ ] **Step 2: Delete sync scripts**

```bash
git rm sync_rtsp_urls.sh
git rm sync_snapshots.sh
```

- [ ] **Step 3: Update deploy.sh — remove NAS SCP**

Remove the lines that SCP index.html and docs.html to the NAS.

- [ ] **Step 4: Update verify.sh — remove NAS references**

Remove any NAS connectivity checks.

- [ ] **Step 5: Commit**

```bash
git add -A
git commit -m "chore: retire NAS sync scripts (iMac is self-contained)"
```

---

### Task 5: Clean up NAS

- [ ] **Step 1: Stop bird containers on NAS**

```bash
ssh -p 2000 vives@192.168.5.92 "
sudo /usr/local/bin/docker stop go2rtc birds-share 2>/dev/null
sudo /usr/local/bin/docker rm go2rtc birds-share 2>/dev/null
echo 'Stopped go2rtc and birds-share'
"
```

- [ ] **Step 2: Kill NAS bird_snapshots.py**

```bash
ssh -p 2000 vives@192.168.5.92 "
sudo kill \$(pgrep -f bird_snapshots.py) 2>/dev/null
echo 'Stopped bird_snapshots.py'
"
```

- [ ] **Step 3: Remove bird cron jobs from NAS**

```bash
ssh -p 2000 vives@192.168.5.92 "
sudo cp /etc/crontab /etc/crontab.bak.\$(date +%s)
sudo sed -i '/refresh_unifi_streams/d' /etc/crontab
sudo sed -i '/bird_snapshots/d' /etc/crontab
sudo sed -i '/export_birdnet/d' /etc/crontab
sudo sed -i '/birdnet_sse/d' /etc/crontab
echo 'Removed bird cron jobs'
cat /etc/crontab
"
```

- [ ] **Step 4: Remove dead unifi stuff from docker-compose**

Remove the `unifi-internal` network and any commented-out unifi/mongo containers from `/volume1/docker/docker-compose.yaml`.

- [ ] **Step 5: Verify NAS is clean**

```bash
ssh -p 2000 vives@192.168.5.92 "
echo '=== Running containers ==='
sudo /usr/local/bin/docker ps --format '{{.Names}} {{.Status}}'
echo ''
echo '=== Bird crons ==='
grep -i bird /etc/crontab || echo 'None'
echo ''
echo '=== Bird processes ==='
ps aux | grep -i bird | grep -v grep || echo 'None'
"
```

---

### Task 6: Final verification

- [ ] **Step 1: Run verify.sh**

```bash
~/bird-classifier/scripts/verify.sh
```

All checks should pass.

- [ ] **Step 2: Test camera through tunnel**

Open `https://birds.vivessato.com` and verify camera feed loads.

- [ ] **Step 3: Test audio is still flowing**

```bash
sqlite3 ~/bird-snapshots/birdnet-audio/birdnet_local.db \
  "SELECT source, MAX(time) FROM notes WHERE date='$(date +%Y-%m-%d)' GROUP BY source;"
```

- [ ] **Step 4: Commit and push**

```bash
git push origin main
```

- [ ] **Step 5: Tag release**

```bash
git tag v1.1-nas-independent
```
