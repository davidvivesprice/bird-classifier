"""UniFi Protect motion events → `unifi_events` in pipeline.db, a recall oracle.

Protect's own motion detector fires on any movement at the feeder, so its
events say when *something* was there regardless of what our detector did:
a motion event with no track of ours is a candidate missed visit, and
(first track start − motion start) says whether we lead or lag it.

Recording side (`main`): one long-lived WebSocket subscription to the
Protect Integration API, upserting events for the mapped cameras. Comparison
side (`compare_with_tracks`): the query behind GET /api/unifi-compare.

Frame shapes observed on Protect 7.2.105 (passive probe, 2026-09-18):
  {"type": "add",    "item": {"id": <uuid>, "modelKey": "event", "type": "motion",
                              "start": <ms>, "device": <cameraId>}}
  {"type": "update", "item": {... same item ..., "end": <ms>}}
Both carry the full item; `end` is absent until the event closes.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import sqlite3
import ssl
import sys
import time
from bisect import bisect_left, bisect_right
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Optional

log = logging.getLogger("unifi-events")

PROTECT_HOST = os.environ.get("PROTECT_HOST", "192.168.4.9")
EVENTS_URL = f"wss://{PROTECT_HOST}/proxy/protect/integration/v1/subscribe/events"
DB_PATH = Path(os.environ.get("UNIFI_EVENTS_DB")
               or Path.home() / "bird-snapshots" / "logs" / "pipeline.db")
HEALTH_PATH = Path("/tmp/unifi-events-health.json")

# Protect camera id → pipeline camera name (pipeline_tracks.camera). The id is
# the "Birds" camera in tools/refresh_rtsp.py CAMERAS.
FEEDER_CAMERA_ID = "690e999401027503e400043b"
CAMERA_NAMES = {FEEDER_CAMERA_ID: "feeder"}

RETENTION_DAYS = 30               # same horizon as PIPELINE_TRACKS_RETENTION_DAYS
OPEN_EVENT_DEFAULT_MS = 10_000    # an event whose `end` never arrived (disconnect mid-event)
DEFAULT_TOLERANCE_MS = 3_000
HOUR_MS = 3_600_000

SCHEMA_UNIFI_EVENTS = """
CREATE TABLE IF NOT EXISTS unifi_events (
    id TEXT PRIMARY KEY,
    camera TEXT NOT NULL,
    type TEXT NOT NULL,
    start_ms INTEGER NOT NULL,
    end_ms INTEGER,
    smart_types TEXT,
    raw_json TEXT,
    updated_ms INTEGER NOT NULL
)
"""

INDEX_UNIFI_EVENTS = (
    "CREATE INDEX IF NOT EXISTS idx_unifi_events_camera_start "
    "ON unifi_events(camera, start_ms)"
)

UPSERT_EVENT = """
INSERT INTO unifi_events
(id, camera, type, start_ms, end_ms, smart_types, raw_json, updated_ms)
VALUES (?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT(id) DO UPDATE SET
    camera = excluded.camera,
    type = excluded.type,
    start_ms = excluded.start_ms,
    end_ms = COALESCE(excluded.end_ms, unifi_events.end_ms),
    smart_types = COALESCE(excluded.smart_types, unifi_events.smart_types),
    raw_json = excluded.raw_json,
    updated_ms = excluded.updated_ms
"""

UPDATE_EVENT = """
UPDATE unifi_events SET
    type = COALESCE(?, type),
    start_ms = COALESCE(?, start_ms),
    end_ms = COALESCE(?, end_ms),
    smart_types = COALESCE(?, smart_types),
    raw_json = ?,
    updated_ms = ?
WHERE id = ?
"""


def _now_ms() -> int:
    return int(time.time() * 1000)


def _iso(ms: Optional[int]) -> Optional[str]:
    if ms is None:
        return None
    return datetime.fromtimestamp(ms / 1000).strftime("%Y-%m-%dT%H:%M:%S")


def parse_message(msg) -> Optional[dict]:
    """Normalise one WebSocket frame to a flat dict, or None if it is not an
    event we understand. Missing fields come back as None rather than raising
    so a partial `update` can still be applied to a row we already hold."""
    if isinstance(msg, (str, bytes, bytearray)):
        try:
            msg = json.loads(msg)
        except (ValueError, TypeError):
            return None
    if not isinstance(msg, dict):
        return None
    item = msg.get("item")
    if not isinstance(item, dict):
        return None
    if item.get("modelKey") not in (None, "event"):
        return None
    event_id = item.get("id")
    if not isinstance(event_id, str) or not event_id:
        return None
    smart = item.get("smartDetectTypes")
    if not isinstance(smart, list):
        smart = None
    return {
        "action": msg.get("type"),
        "id": event_id,
        "device": item.get("device") or item.get("camera"),
        "type": item.get("type"),
        "start_ms": _int_or_none(item.get("start")),
        "end_ms": _int_or_none(item.get("end")),
        "smart_types": smart,
        "item": item,
    }


def _int_or_none(v) -> Optional[int]:
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    return int(v)


class UnifiEventStore:
    """Own WAL connection to pipeline.db — never the pipeline's EventStore
    instance, which lives in another process."""

    def __init__(self, db_path: str, cameras: Optional[dict] = None):
        self.cameras = dict(CAMERA_NAMES if cameras is None else cameras)
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(db_path, timeout=5, check_same_thread=False)
        self.conn.execute("PRAGMA busy_timeout=5000")
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.execute(SCHEMA_UNIFI_EVENTS)
        self.conn.execute(INDEX_UNIFI_EVENTS)
        self.conn.commit()

    def close(self):
        try:
            self.conn.close()
        except sqlite3.Error:
            pass

    def apply(self, ev: dict, now_ms: Optional[int] = None) -> bool:
        """Upsert one parsed event. Returns True if a row was written."""
        now_ms = _now_ms() if now_ms is None else now_ms
        smart = json.dumps(ev["smart_types"]) if ev["smart_types"] is not None else None
        raw = json.dumps(ev["item"], separators=(",", ":"))
        device = ev["device"]
        camera = self.cameras.get(device) if device is not None else None
        if device is not None and camera is None:
            return False
        if camera is None or ev["start_ms"] is None or ev["type"] is None:
            cur = self.conn.execute(UPDATE_EVENT, (
                ev["type"], ev["start_ms"], ev["end_ms"], smart, raw, now_ms, ev["id"]))
            self.conn.commit()
            return cur.rowcount > 0
        self.conn.execute(UPSERT_EVENT, (
            ev["id"], camera, ev["type"], ev["start_ms"], ev["end_ms"],
            smart, raw, now_ms))
        self.conn.commit()
        return True

    def prune(self, older_than_ms: int) -> int:
        cur = self.conn.execute(
            "DELETE FROM unifi_events WHERE start_ms < ?", (int(older_than_ms),))
        self.conn.commit()
        return cur.rowcount


# ── Comparison ───────────────────────────────────────────────────────────────

def _percentile(sorted_vals: list, pct: int) -> Optional[int]:
    """Nearest-rank percentile; integer arithmetic so 0.9*10 can't round up."""
    n = len(sorted_vals)
    if not n:
        return None
    idx = (pct * n + 99) // 100 - 1
    return sorted_vals[min(max(idx, 0), n - 1)]


def earliest_event_ms(conn: sqlite3.Connection, camera: str) -> Optional[int]:
    """Start of the oldest stored event for `camera`, or None when there is
    none. Together with the oracle's run start this bounds its coverage, so
    the compare window is never widened over time the oracle was not
    recording. Raises sqlite3.OperationalError when the table does not exist."""
    row = conn.execute(
        "SELECT MIN(start_ms) FROM unifi_events WHERE camera = ?", (camera,)).fetchone()
    return None if row is None or row[0] is None else int(row[0])


def compare_with_tracks(conn: sqlite3.Connection, camera: str, since_ms: int,
                        until_ms: int, tolerance_ms: int = DEFAULT_TOLERANCE_MS,
                        event_type: str = "motion", max_items: int = 50) -> dict:
    """Join Protect events to pipeline_tracks over [since_ms, until_ms].

    Events and tracks are both selected by overlap with the window (an event
    still open at since_ms counts, like the track it produced), so an edge
    can't turn a covered track into a phantom. An event is covered when any
    track overlaps it, padded by tolerance_ms on both sides; lead/lag is
    (first overlapping track start − motion start), so negative means we saw
    the bird before Protect did. Raises sqlite3.OperationalError when the
    unifi_events table does not exist yet.
    """
    rows = conn.execute(
        "SELECT id, type, start_ms, end_ms FROM unifi_events "
        "WHERE camera = ? AND COALESCE(end_ms, start_ms + ?) >= ? AND start_ms <= ? "
        "ORDER BY start_ms",
        (camera, OPEN_EVENT_DEFAULT_MS, int(since_ms), int(until_ms))).fetchall()
    by_type = Counter(r[1] for r in rows)
    events = [(eid, s, e, s + OPEN_EVENT_DEFAULT_MS if e is None else e)
              for (eid, etype, s, e) in rows if etype == event_type]
    tracks = conn.execute(
        "SELECT track_id, species, start_time, end_time FROM pipeline_tracks "
        "WHERE camera = ? AND end_time >= ? AND start_time <= ? ORDER BY start_time",
        (camera, int(since_ms), int(until_ms))).fetchall()

    starts = [t[2] for t in tracks]
    max_len = max((t[3] - t[2] for t in tracks), default=0)
    matched: set = set()
    lags: list = []
    misses: list = []
    hours: dict = {}

    def bucket(ms):
        h = ms // HOUR_MS * HOUR_MS
        return hours.setdefault(h, {"hour_ms": h, "hour": _iso(h)[:13] + ":00",
                                    "motion": 0, "covered": 0, "tracks": 0, "phantoms": 0})

    for eid, s, e_raw, e_eff in events:
        lo = bisect_left(starts, s - tolerance_ms - max_len)
        hi = bisect_right(starts, e_eff + tolerance_ms)
        first = None
        for tid, _sp, ts, te in tracks[lo:hi]:
            if te >= s - tolerance_ms and ts <= e_eff + tolerance_ms:
                matched.add(tid)
                first = ts if first is None else min(first, ts)
        b = bucket(s)
        b["motion"] += 1
        if first is None:
            misses.append({"id": eid, "start_ms": s, "end_ms": e_raw,
                           "duration_ms": None if e_raw is None else e_raw - s,
                           "start": _iso(s)})
        else:
            lags.append(first - s)
            b["covered"] += 1

    phantoms = []
    for tid, sp, ts, te in tracks:
        b = bucket(ts)
        b["tracks"] += 1
        if tid not in matched:
            b["phantoms"] += 1
            phantoms.append({"track_id": tid, "species": sp, "start_ms": ts, "end_ms": te,
                             "duration_ms": te - ts, "start": _iso(ts)})

    lags.sort()
    misses.reverse()
    phantoms.reverse()
    covered = len(events) - len(misses)
    return {
        "camera": camera,
        "since_ms": int(since_ms),
        "until_ms": int(until_ms),
        "tolerance_ms": int(tolerance_ms),
        "event_type": event_type,
        "motion_events": len(events),
        "by_type": dict(by_type),
        "events_with_track": covered,
        "coverage": (covered / len(events)) if events else None,
        "events_without_track": {"count": len(misses), "items": misses[:max_items]},
        "tracks_total": len(tracks),
        "tracks_without_motion": {"count": len(phantoms), "items": phantoms[:max_items]},
        "lead_lag_ms": {"p50": _percentile(lags, 50), "p90": _percentile(lags, 90),
                        "n": len(lags)},
        "hours": [hours[k] for k in sorted(hours)],
    }


# ── Subscriber ───────────────────────────────────────────────────────────────

class Health:
    def __init__(self, path: Path = HEALTH_PATH):
        self.path = path
        now = _now_ms()
        self.state = {
            "service": "unifi-events", "status": "starting", "host": PROTECT_HOST,
            "started_ms": now, "connected_since_ms": None,
            "updated_ms": now, "updated": _iso(now),
            "frames_seen": 0, "events_stored": 0, "unknown_frames": 0,
            "reconnects": 0, "last_event_ms": None, "last_error": None,
        }
        self._write_failed = False

    def set(self, **fields):
        self.state.update(fields)
        self.write()

    def write(self):
        now = _now_ms()
        self.state["updated_ms"] = now
        self.state["updated"] = _iso(now)
        try:
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.state))
            tmp.replace(self.path)
            self._write_failed = False
        except OSError as e:
            if not self._write_failed:
                log.warning("health write failed: %s", e)
            self._write_failed = True


class Subscriber:
    UNKNOWN_LOG_FIRST = 20
    UNKNOWN_LOG_EVERY = 100

    def __init__(self, store: UnifiEventStore, health: Health, api_key: str,
                 url: str = EVENTS_URL):
        self.store = store
        self.health = health
        self.api_key = api_key
        self.url = url

    def handle_frame(self, raw) -> bool:
        st = self.health.state
        st["frames_seen"] += 1
        ev = parse_message(raw)
        if ev is None:
            st["unknown_frames"] += 1
            n = st["unknown_frames"]
            if n <= self.UNKNOWN_LOG_FIRST or n % self.UNKNOWN_LOG_EVERY == 0:
                log.warning("unknown frame #%d: %s", n, _shape(raw))
            return False
        stored = self.store.apply(ev)
        if stored:
            st["events_stored"] += 1
            st["last_event_ms"] = ev["start_ms"] or _now_ms()
        return stored

    async def run(self):
        import websockets

        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        backoff = 1.0
        while True:
            opened = time.monotonic()
            try:
                async with websockets.connect(
                    self.url, additional_headers={"X-API-KEY": self.api_key}, ssl=ctx,
                    open_timeout=15, ping_interval=30, ping_timeout=30, max_size=1 << 20,
                ) as ws:
                    self.health.set(status="connected", connected_since_ms=_now_ms(),
                                    last_error=None)
                    log.info("connected to %s", PROTECT_HOST)
                    async for raw in ws:
                        try:
                            self.handle_frame(raw)
                        except Exception:
                            log.exception("frame handling failed")
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self.health.state["last_error"] = f"{type(e).__name__}: {e}"[:200]
                log.warning("connection lost: %s: %s", type(e).__name__, e)
            if time.monotonic() - opened > 60:
                backoff = 1.0
            self.health.state["reconnects"] += 1
            self.health.set(status="reconnecting", connected_since_ms=None)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60.0)

    async def housekeeping(self, interval_s: float = 60.0, prune_every_s: float = 3600.0):
        last_prune = 0.0
        while True:
            await asyncio.sleep(interval_s)
            self.health.write()
            if time.monotonic() - last_prune >= prune_every_s:
                last_prune = time.monotonic()
                try:
                    n = self.store.prune(_now_ms() - RETENTION_DAYS * 86_400_000)
                    if n:
                        log.info("pruned %d events older than %d days", n, RETENTION_DAYS)
                except sqlite3.Error as e:
                    log.warning("prune failed: %s", e)


def _shape(raw, limit: int = 300) -> str:
    """Keys and value types only — never the values (the frame may carry ids)."""
    try:
        msg = json.loads(raw) if isinstance(raw, (str, bytes, bytearray)) else raw
    except (ValueError, TypeError):
        return f"<non-json {len(raw)} bytes>"

    def walk(o):
        if isinstance(o, dict):
            return {k: walk(v) for k, v in list(o.items())[:20]}
        if isinstance(o, list):
            return [walk(v) for v in o[:3]]
        return type(o).__name__

    return json.dumps(walk(msg))[:limit]


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    api_key = os.environ.get("UNIFI_API_KEY") or os.environ.get("UNIFI_PROTECT_API_KEY")
    health = Health()
    if not api_key:
        health.set(status="misconfigured", last_error="UNIFI_API_KEY not set")
        log.error("UNIFI_API_KEY (or UNIFI_PROTECT_API_KEY) not set — see ~/.bird-observatory-env")
        return 1
    store = UnifiEventStore(str(DB_PATH))
    sub = Subscriber(store, health, api_key=api_key)

    async def _run():
        task = asyncio.gather(sub.run(), sub.housekeeping())
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, task.cancel)
        try:
            await task
        except asyncio.CancelledError:
            pass

    try:
        asyncio.run(_run())
    except Exception as e:
        # e.g. `websockets` missing from the venv, or a task dying on something
        # run() does not catch. Say so in the health file rather than report a
        # clean "stopped" while systemd restart-loops us every RestartSec.
        health.set(status="failed", last_error=f"{type(e).__name__}: {e}"[:200])
        log.exception("subscriber crashed")
        return 1
    finally:
        store.close()
    health.set(status="stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
