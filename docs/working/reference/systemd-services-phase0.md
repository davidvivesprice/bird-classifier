# Phase 0 Systemd Service Definitions — Integrity Audit + RTSP Refresh

## Overview

Two new systemd-user services enable Phase 0 migration prep (shadow deployment launch). These run on the Pi and match iMac LaunchAgent equivalents.

**Status:** Deployed to Pi `/home/vives/.config/systemd/user/` on 2026-04-29. Ready for testing.

---

## 1. bird-integrity-audit.service + bird-integrity-audit.timer

**Purpose:** Hourly database integrity audit (schema + row count validation).

**Unit files:**
- `/home/vives/.config/systemd/user/bird-integrity-audit.service`
- `/home/vives/.config/systemd/user/bird-integrity-audit.timer`

**Schedule:** Hourly (OnCalendar=hourly) with 300-second random delay spread.

**Script:** `~/bird-classifier/tools/integrity_audit.py`
- Checks 3 SQLite databases: `classifications.db`, `pipeline.db`, `pi_reviews.db`
- Environment var `DB_MODE=mirror` gates behavior: during Phase 1 shadow, the script asserts read-only mode (does not attempt writes)
- Logs to systemd journal via stdout

**Activation:**
```bash
ssh vives@pi5.local "systemctl --user enable bird-integrity-audit.timer && \
  systemctl --user start bird-integrity-audit.timer"
```

**Check status:**
```bash
ssh vives@pi5.local "systemctl --user list-timers bird-integrity-audit* && \
  journalctl --user -u bird-integrity-audit -n 20"
```

**Critical note for Phase 1:** During shadow deployment (Phase 1), the Pi receives a read-only Litestream mirror of iMac's databases. The audit script must run against that mirror, not attempt to write. The `DB_MODE=mirror` env var enforces this in the service unit.

---

## 2. refresh-rtsp.service + refresh-rtsp.timer

**Purpose:** Daily RTSP stream refresh at 3:10 AM (clears stale connections, restarts go2rtc).

**Unit files:**
- `/home/vives/.config/systemd/user/refresh-rtsp.service`
- `/home/vives/.config/systemd/user/refresh-rtsp.timer`

**Schedule:** Daily at 03:10:00 (OnCalendar=*-*-* 03:10:00).

**Script:** `~/bird-classifier/tools/refresh_rtsp.py`
- Calls `systemctl --user restart go2rtc`
- On Pi, UNIFI_API_KEY is stable; this is a prophylactic measure to clear connection pools

**Activation:**
```bash
ssh vives@pi5.local "systemctl --user enable refresh-rtsp.timer && \
  systemctl --user start refresh-rtsp.timer"
```

**Check status:**
```bash
ssh vives@pi5.local "systemctl --user list-timers refresh-rtsp* && \
  journalctl --user -u refresh-rtsp -n 20"
```

**🚩 CRITICAL HANDOVER GATE — Phase 1 ↔ Phase 2 cutover:**

During Phase 1 shadow (visual cutover), **the iMac owns audio and runs its own go2rtc for audio multiplexing**. The Pi's `refresh-rtsp.timer` must be **masked (disabled)** to avoid conflicting restarts:

```bash
# At Phase 1 start (Pi takes visual):
ssh vives@pi5.local "systemctl --user mask refresh-rtsp.timer refresh-rtsp.service"

# At Phase 1 → Phase 2 boundary (Pi takes audio):
ssh vives@pi5.local "systemctl --user unmask refresh-rtsp.timer && \
  systemctl --user enable refresh-rtsp.timer && \
  systemctl --user start refresh-rtsp.timer"
```

This explicit handover prevents two systems from restarting the same RTSP pipeline during the shadow window.

---

## Testing

Run both services manually to verify script correctness before the timer fires:

```bash
# Integrity audit
ssh vives@pi5.local "systemctl --user start bird-integrity-audit.service --wait"

# RTSP refresh
ssh vives@pi5.local "systemctl --user start refresh-rtsp.service --wait"
```

Check logs:
```bash
ssh vives@pi5.local "journalctl --user -n 50 --no-pager | grep -E 'integrity|refresh-rtsp'"
```

---

## Files deployed

| File | Path | Status |
|---|---|---|
| Service unit (audit) | `~/.config/systemd/user/bird-integrity-audit.service` | ✓ deployed |
| Timer unit (audit) | `~/.config/systemd/user/bird-integrity-audit.timer` | ✓ deployed |
| Service unit (RTSP) | `~/.config/systemd/user/refresh-rtsp.service` | ✓ deployed |
| Timer unit (RTSP) | `~/.config/systemd/user/refresh-rtsp.timer` | ✓ deployed |
| Script (audit) | `~/bird-classifier/tools/integrity_audit.py` | ✓ deployed |
| Script (RTSP) | `~/bird-classifier/tools/refresh_rtsp.py` | ✓ deployed |

---

## Next steps

1. Activate timers: `systemctl --user enable --now bird-integrity-audit.timer refresh-rtsp.timer`
2. Monitor logs for first few fires
3. At Phase 0 → Phase 1 boundary: mask refresh-rtsp per handover gate above
4. At Phase 1 → Phase 2 boundary: unmask refresh-rtsp per handover gate above

See `~/bird-classifier-pi/docs/working/progress/cross-claude-comms.md` for phase cutover coordination.
