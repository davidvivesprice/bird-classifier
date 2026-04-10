"""EventStore — time-indexed SQLite WAL for pipeline events and tracks."""
from __future__ import annotations
import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Optional

from pipeline.tracker import Track


SCHEMA_EVENTS = """
CREATE TABLE IF NOT EXISTS pipeline_events (
    camera TEXT NOT NULL,
    frame_time INTEGER NOT NULL,
    track_id INTEGER NOT NULL,
    species TEXT,
    confidence REAL,
    model_source TEXT,
    bbox_json TEXT NOT NULL,
    is_new INTEGER DEFAULT 0,
    PRIMARY KEY (camera, frame_time, track_id)
)
"""

SCHEMA_TRACKS = """
CREATE TABLE IF NOT EXISTS pipeline_tracks (
    track_id INTEGER PRIMARY KEY AUTOINCREMENT,
    camera TEXT NOT NULL,
    species TEXT,
    start_time INTEGER NOT NULL,
    end_time INTEGER NOT NULL,
    peak_confidence REAL,
    num_frames INTEGER,
    model_source TEXT,
    best_keeper_path TEXT,
    motion_pct REAL
)
"""

INDEX_STATEMENTS = [
    "CREATE INDEX IF NOT EXISTS idx_events_track ON pipeline_events(camera, track_id)",
    "CREATE INDEX IF NOT EXISTS idx_events_time ON pipeline_events(frame_time)",
    "CREATE INDEX IF NOT EXISTS idx_events_species ON pipeline_events(species, frame_time)",
    "CREATE INDEX IF NOT EXISTS idx_tracks_species ON pipeline_tracks(species, start_time)",
    "CREATE INDEX IF NOT EXISTS idx_tracks_duration ON pipeline_tracks(camera, end_time, start_time)",
]

INSERT_EVENT = """
INSERT OR REPLACE INTO pipeline_events
(camera, frame_time, track_id, species, confidence, model_source, bbox_json, is_new)
VALUES (?, ?, ?, ?, ?, ?, ?, ?)
"""

INSERT_TRACK = """
INSERT INTO pipeline_tracks
(camera, species, start_time, end_time, peak_confidence, num_frames,
 model_source, best_keeper_path, motion_pct)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
"""


class EventStore:
    def __init__(self, db_path: str, flush_interval_s: float = 0.5,
                 batch_size: int = 50):
        self.db_path = db_path
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn_lock = threading.Lock()
        self.conn = sqlite3.connect(db_path, check_same_thread=False, timeout=5)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.execute("PRAGMA wal_autocheckpoint=2000")
        self.conn.execute(SCHEMA_EVENTS)
        self.conn.execute(SCHEMA_TRACKS)
        for idx in INDEX_STATEMENTS:
            self.conn.execute(idx)
        self.conn.commit()

        self._event_batch: list = []
        self._batch_lock = threading.Lock()
        self._batch_size = batch_size
        self._flush_interval = flush_interval_s
        self._stop = threading.Event()
        self._flusher = threading.Thread(
            target=self._flush_loop, name="event-store-flush", daemon=True
        )
        self._flusher.start()

    def shutdown(self):
        self._stop.set()
        self._flusher.join(timeout=2)
        self.flush()
        with self._conn_lock:
            try:
                self.conn.close()
            except Exception:
                pass

    def _flush_loop(self):
        while not self._stop.is_set():
            time.sleep(self._flush_interval)
            try:
                self.flush()
            except Exception:
                pass  # never kill the flusher thread

    def flush(self):
        with self._batch_lock:
            if not self._event_batch:
                return
            batch = self._event_batch
            self._event_batch = []
        with self._conn_lock:
            self.conn.executemany(INSERT_EVENT, batch)
            self.conn.commit()

    def write_event(self, camera: str, frame_time_ms: float, track_id: int,
                    species: Optional[str], confidence: float,
                    model_source: Optional[str], bbox: list, is_new: bool):
        row = (
            camera, int(frame_time_ms), int(track_id), species,
            float(confidence or 0), model_source,
            json.dumps(bbox), int(1 if is_new else 0),
        )
        batch_ready: Optional[list] = None
        with self._batch_lock:
            self._event_batch.append(row)
            if len(self._event_batch) >= self._batch_size:
                batch_ready = self._event_batch
                self._event_batch = []
        if batch_ready is not None:
            with self._conn_lock:
                self.conn.executemany(INSERT_EVENT, batch_ready)
                self.conn.commit()

    def write_track_summary(self, camera: str, track: Track, num_frames: int):
        # Motion %: fraction of motion_history transitions showing movement > 5px
        motion_pct = 0.0
        hist = list(track.motion_history)
        if len(hist) >= 2:
            moves = sum(
                1 for (a, b) in zip(hist, hist[1:])
                if abs(a[0] - b[0]) > 5 or abs(a[1] - b[1]) > 5
            )
            motion_pct = moves / max(1, len(hist) - 1)
        row = (
            camera, track.species,
            int(track.created_at_ms), int(track.last_updated_ms),
            float(track.confidence or 0), int(num_frames),
            track.model_source, None, float(motion_pct),
        )
        with self._conn_lock:
            self.conn.execute(INSERT_TRACK, row)
            self.conn.commit()

    def query_events(self, camera: str, start_ms: int, end_ms: int) -> list:
        with self._conn_lock:
            cur = self.conn.execute(
                """SELECT camera, frame_time, track_id, species, confidence,
                           model_source, bbox_json, is_new
                   FROM pipeline_events
                   WHERE camera = ? AND frame_time BETWEEN ? AND ?
                   ORDER BY frame_time ASC""",
                (camera, start_ms, end_ms),
            )
            cols = [c[0] for c in cur.description]
            return [dict(zip(cols, row)) for row in cur.fetchall()]

    def query_tracks(self, camera: Optional[str] = None,
                     species: Optional[str] = None,
                     start_ms: Optional[int] = None,
                     end_ms: Optional[int] = None,
                     min_duration_s: Optional[float] = None,
                     min_confidence: Optional[float] = None,
                     limit: int = 100) -> list:
        clauses = []
        params = []
        if camera:
            clauses.append("camera = ?"); params.append(camera)
        if species:
            clauses.append("species = ?"); params.append(species)
        if start_ms is not None:
            clauses.append("start_time >= ?"); params.append(int(start_ms))
        if end_ms is not None:
            clauses.append("end_time <= ?"); params.append(int(end_ms))
        if min_duration_s is not None:
            clauses.append("(end_time - start_time) >= ?")
            params.append(int(min_duration_s * 1000))
        if min_confidence is not None:
            clauses.append("peak_confidence >= ?"); params.append(min_confidence)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        sql = (
            "SELECT track_id, camera, species, start_time, end_time, "
            "peak_confidence, num_frames, model_source, motion_pct "
            "FROM pipeline_tracks" + where +
            " ORDER BY start_time DESC LIMIT ?"
        )
        params.append(limit)
        with self._conn_lock:
            cur = self.conn.execute(sql, params)
            cols = [c[0] for c in cur.description]
            return [dict(zip(cols, row)) for row in cur.fetchall()]

    def prune_events(self, older_than_ms: int):
        with self._conn_lock:
            self.conn.execute(
                "DELETE FROM pipeline_events WHERE frame_time < ?",
                (older_than_ms,),
            )
            self.conn.commit()

    def daily_checkpoint(self):
        with self._conn_lock:
            try:
                self.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            except Exception:
                pass
