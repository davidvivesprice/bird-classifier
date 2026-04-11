"""HTTP SSE event server for live track events.

Serves per-frame track events over Server-Sent Events on GET /events/sse?camera=<name>.
Events are dropped for slow clients (queue overflow) rather than blocking the emitter.

Also exposes GET /health returning {"ok": true} for liveness checks.
"""
from __future__ import annotations
import json
import logging
import queue
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Optional
from urllib.parse import urlparse, parse_qs

log = logging.getLogger(__name__)

CLIENT_QUEUE_MAX = 32
KEEPALIVE_INTERVAL_S = 0.5


class _SSEHandler(BaseHTTPRequestHandler):
    # Populated by SSEEventServer before serving starts
    server_state: "SSEEventServer" = None  # type: ignore

    def log_message(self, format, *args):
        # Silence the default stderr logger
        return

    def do_GET(self):  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path == "/health":
            body = b'{"ok": true}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if parsed.path == "/events/sse":
            qs = parse_qs(parsed.query)
            cameras = qs.get("camera", [])
            if not cameras:
                body = b"missing ?camera="
                self.send_response(400)
                self.send_header("Content-Type", "text/plain")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            self._stream_events(cameras[0])
            return
        self.send_response(404)
        self.end_headers()

    def _stream_events(self, camera: str):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()

        q: "queue.Queue[str]" = queue.Queue(maxsize=CLIENT_QUEUE_MAX)
        self.server_state._add_client(camera, q)
        try:
            while True:
                try:
                    payload = q.get(timeout=KEEPALIVE_INTERVAL_S)
                except queue.Empty:
                    self.wfile.write(b": keepalive\n\n")
                    self.wfile.flush()
                    continue
                self.wfile.write(f"data: {payload}\n\n".encode("utf-8"))
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:
            log.debug("SSE client disconnected: %s", e)
        finally:
            self.server_state._remove_client(camera, q)


class SSEEventServer:
    """SSE broadcaster for per-frame track events.

    Usage:
        server = SSEEventServer(port=8102)
        server.start()
        server.emit(camera="feeder", wall_time_ms=..., tracks=[...])
        server.stop()
    """

    def __init__(self, port: int = 8102, host: str = "127.0.0.1"):
        self.port = port
        self.host = host
        self._clients: dict[str, list[queue.Queue]] = {}
        self._clients_lock = threading.Lock()
        self._httpd: Optional[ThreadingHTTPServer] = None
        self._thread: Optional[threading.Thread] = None
        self.stats = {"events_emitted": 0, "clients_connected": 0}

    def start(self) -> None:
        handler_cls = _SSEHandler
        handler_cls.server_state = self
        self._httpd = ThreadingHTTPServer((self.host, self.port), handler_cls)
        self._thread = threading.Thread(
            target=self._httpd.serve_forever,
            name="sse-events",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        if self._httpd is not None:
            self._httpd.shutdown()
            self._httpd.server_close()
            self._httpd = None

    def emit(self, camera: str, wall_time_ms: int, tracks: list) -> None:
        payload = json.dumps({
            "camera": camera,
            "wall_time_ms": wall_time_ms,
            "tracks": tracks,
        })
        with self._clients_lock:
            cams = list(self._clients.get(camera, []))
        for q in cams:
            try:
                q.put_nowait(payload)
            except queue.Full:
                pass  # slow client — drop
        self.stats["events_emitted"] += 1

    def _add_client(self, camera: str, q: "queue.Queue") -> None:
        with self._clients_lock:
            self._clients.setdefault(camera, []).append(q)
            self.stats["clients_connected"] += 1

    def _remove_client(self, camera: str, q: "queue.Queue") -> None:
        with self._clients_lock:
            if camera in self._clients and q in self._clients[camera]:
                self._clients[camera].remove(q)
