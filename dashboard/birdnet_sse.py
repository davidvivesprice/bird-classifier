#!/usr/bin/env python3
"""BirdNET real-time SSE relay + audio clip server.

Polls BirdNET-Go SQLite DB every 3 seconds for new detections and pushes
them as Server-Sent Events (SSE) to connected browsers. Also serves WAV
audio clips from the BirdNET Docker volume.

Designed for Python 3.8+ stdlib only (no pip dependencies).
Runs on the NAS host (not inside Docker) for direct filesystem access.

Usage: python3 birdnet_sse.py
"""

import json
import logging
import mimetypes
import queue
import sqlite3
import sys
import threading
import time
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from socketserver import ThreadingMixIn

# ── Configuration ──
PORT = 8098
POLL_INTERVAL = 3  # seconds between DB polls

# BirdNET-Go Docker volume paths
DOCKER_VOLUME = Path(
    "/volume1/@docker/volumes/"
    "35bfc1d58780095a9ef22a84b6ca2524ef9b2eea7d690dfc039b2698a275f80b/_data"
)
DB_PATH = DOCKER_VOLUME / "birdnet.db"
CLIPS_DIR = DOCKER_VOLUME / "clips"

# Logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("birdnet_sse")

# ── Recent detections cache ──
_recent_cache = None
_recent_cache_time = 0
RECENT_CACHE_TTL = 60  # seconds

# ── SSE Client Registry ──
# Each client is a Queue; the per-client thread reads from it and writes to wfile.
client_queues = []
clients_lock = threading.Lock()


def broadcast_event(message_bytes):
    """Put an SSE message into every client's queue."""
    with clients_lock:
        for q in client_queues:
            try:
                q.put_nowait(message_bytes)
            except queue.Full:
                pass  # Client too slow; skip this event


# ── SQLite Poller ──
def poll_loop():
    """Background thread: poll BirdNET DB for new detections."""
    last_id = 0

    # Get the current max ID so we only stream NEW detections
    while True:
        try:
            conn = sqlite3.connect(str(DB_PATH), timeout=5)
            conn.execute("PRAGMA journal_mode=WAL")
            cur = conn.execute("SELECT MAX(id) FROM notes")
            row = cur.fetchone()
            last_id = row[0] if row and row[0] else 0
            conn.close()
            log.info("Starting from detection ID %d", last_id)
            break
        except Exception as e:
            log.error("Cannot connect to DB: %s — retrying in 5s", e)
            time.sleep(5)

    while True:
        time.sleep(POLL_INTERVAL)
        try:
            conn = sqlite3.connect(str(DB_PATH), timeout=5)
            conn.execute("PRAGMA journal_mode=WAL")
            cur = conn.execute(
                "SELECT id, common_name, scientific_name, confidence, "
                "date, time, clip_name "
                "FROM notes WHERE id > ? ORDER BY id ASC",
                (last_id,),
            )
            rows = cur.fetchall()
            conn.close()

            for row in rows:
                det_id, common_name, sci_name, conf, date, time_str, clip_name = row
                event = {
                    "id": det_id,
                    "common_name": common_name,
                    "scientific_name": sci_name,
                    "confidence": round(conf, 3) if conf else 0,
                    "date": date,
                    "time": time_str,
                    "clip_name": clip_name or "",
                }
                log.info("New detection: %s (%.0f%%) — %s", common_name, conf * 100, clip_name or "no clip")
                msg = f"data: {json.dumps(event)}\n\n".encode("utf-8")
                broadcast_event(msg)
                last_id = det_id

        except sqlite3.OperationalError as e:
            if "locked" in str(e).lower():
                log.warning("DB locked, will retry next cycle")
            else:
                log.error("DB error: %s", e)
        except Exception as e:
            log.error("Poll error: %s", e)


# ── HTTP Handler ──
class SSEHandler(BaseHTTPRequestHandler):
    """Handle SSE, clip serving, and health check requests."""

    # Suppress default access log (we do our own logging)
    def log_message(self, format, *args):
        pass

    def do_GET(self):
        if self.path == "/events":
            self.handle_sse()
        elif self.path.startswith("/clips/"):
            self.handle_clip()
        elif self.path == "/health":
            self.handle_health()
        elif self.path == "/recent":
            self.handle_recent()
        else:
            self.send_error(404)

    def handle_sse(self):
        """SSE stream endpoint — one thread per client, queue-based."""
        log.info("SSE client connected from %s", self.client_address[0])
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()

        # Send initial keepalive
        try:
            self.wfile.write(b": connected\n\n")
            self.wfile.flush()
        except Exception:
            return

        # Create per-client queue
        q = queue.Queue(maxsize=100)
        with clients_lock:
            client_queues.append(q)
            log.info("SSE clients: %d", len(client_queues))

        try:
            while True:
                try:
                    # Wait for an event or timeout (heartbeat every 15s)
                    msg = q.get(timeout=15)
                    self.wfile.write(msg)
                    self.wfile.flush()
                except queue.Empty:
                    # Send heartbeat to detect dead connections
                    self.wfile.write(b": heartbeat\n\n")
                    self.wfile.flush()
        except Exception:
            pass
        finally:
            with clients_lock:
                if q in client_queues:
                    client_queues.remove(q)
            log.info("SSE client disconnected, %d remaining", len(client_queues))

    def handle_clip(self):
        """Serve audio clips from BirdNET Docker volume."""
        # /clips/2026/03/foo.wav → CLIPS_DIR/2026/03/foo.wav
        rel_path = self.path[len("/clips/"):]
        # Security: prevent directory traversal
        if ".." in rel_path or rel_path.startswith("/"):
            self.send_error(400, "Invalid path")
            return

        clip_path = CLIPS_DIR / rel_path
        if not clip_path.exists() or not clip_path.is_file():
            self.send_error(404, "Clip not found")
            return

        # Determine content type
        content_type, _ = mimetypes.guess_type(str(clip_path))
        if not content_type:
            content_type = "audio/wav"

        try:
            data = clip_path.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "public, max-age=86400")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(data)
        except Exception as e:
            log.error("Error serving clip %s: %s", rel_path, e)
            self.send_error(500)

    def handle_recent(self):
        """Return recent BirdNET detections (last 7 days), cached 60s."""
        global _recent_cache, _recent_cache_time
        now = time.time()
        if _recent_cache is None or (now - _recent_cache_time) > RECENT_CACHE_TTL:
            try:
                conn = sqlite3.connect(str(DB_PATH), timeout=5)
                conn.execute("PRAGMA journal_mode=WAL")
                cur = conn.execute(
                    "SELECT common_name, ROUND(confidence, 3), date || ' ' || time "
                    "FROM notes WHERE date >= date('now', '-7 days') ORDER BY id DESC"
                )
                rows = cur.fetchall()
                conn.close()
                _recent_cache = json.dumps([
                    {"species": r[0], "confidence": r[1], "time": r[2]}
                    for r in rows
                ])
                _recent_cache_time = now
                log.info("Recent cache refreshed: %d detections", len(rows))
            except Exception as e:
                log.error("Recent query error: %s", e)
                if _recent_cache is None:
                    _recent_cache = "[]"

        body = _recent_cache.encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "public, max-age=60")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def handle_health(self):
        """Health check endpoint."""
        try:
            conn = sqlite3.connect(str(DB_PATH), timeout=3)
            cur = conn.execute("SELECT COUNT(*) FROM notes")
            count = cur.fetchone()[0]
            conn.close()
            body = json.dumps({"status": "ok", "total_detections": count, "sse_clients": len(client_queues)})
        except Exception as e:
            body = json.dumps({"status": "error", "error": str(e)})
            self.send_response(503)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body.encode())
            return

        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body.encode())


class ThreadedHTTPServer(ThreadingMixIn, HTTPServer):
    """Handle each request in a new thread (needed for SSE long-poll)."""
    daemon_threads = True
    allow_reuse_address = True


# ── Main ──
def main():
    if not DB_PATH.exists():
        log.error("BirdNET DB not found at %s", DB_PATH)
        sys.exit(1)

    log.info("Clips directory: %s (exists=%s)", CLIPS_DIR, CLIPS_DIR.exists())

    # Start background poller
    poller = threading.Thread(target=poll_loop, daemon=True)
    poller.start()
    log.info("SQLite poller started (interval=%ds)", POLL_INTERVAL)

    # Start HTTP server
    server = ThreadedHTTPServer(("0.0.0.0", PORT), SSEHandler)
    log.info("SSE server listening on 0.0.0.0:%d", PORT)
    log.info("  /events   — SSE stream")
    log.info("  /clips/   — audio clips")
    log.info("  /health   — health check")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log.info("Shutting down")
        server.shutdown()


if __name__ == "__main__":
    main()
