"""HTTP SSE event server for live track events.

Serves per-frame track events over Server-Sent Events on GET /events/sse?camera=<name>.
Events are dropped for slow clients (queue overflow) rather than blocking the emitter.

Also exposes GET /health returning {"ok": true} for liveness checks.
"""
from __future__ import annotations
import json
import logging
import os
import queue
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Optional
from urllib.parse import urlparse, parse_qs

log = logging.getLogger(__name__)

CLIENT_QUEUE_MAX = 32
KEEPALIVE_INTERVAL_S = 15.0


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
        # Send timeout: without it, a client that stops reading (stalled proxy,
        # suspended laptop, wedged curl) blocks wfile.write() FOREVER — the
        # handler thread stops draining q, the queue fills, and that client
        # receives nothing ever again even if it recovers. With the timeout the
        # stalled connection dies cleanly and the client reconnects. Healthy
        # idle streams are unaffected (keepalives are tiny and ACKed).
        try:
            self.connection.settimeout(10.0)
        except Exception:
            pass
        try:
            while True:
                try:
                    payload = q.get(timeout=self.server_state.keepalive_interval_s)
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
        server = SSEEventServer(port=8105)
        server.start()
        server.emit(camera="feeder", wall_time_ms=..., tracks=[...])
        server.stop()
    """

    def __init__(self, port: int = 8105, host: str = "127.0.0.1", keepalive_interval_s: float = KEEPALIVE_INTERVAL_S):
        self.port = port
        self.host = host
        self.keepalive_interval_s = keepalive_interval_s
        self._clients: dict[str, list[queue.Queue]] = {}
        self._clients_lock = threading.Lock()
        # Monotonic event counter. Clients detect transport drops (bounded
        # per-client queue, proxy stalls) from gaps in seq — without it those
        # losses are silent and transport lag is indistinguishable from
        # pipeline lag.
        self._seq = 0
        self._httpd: Optional[ThreadingHTTPServer] = None
        self._thread: Optional[threading.Thread] = None
        self.stats = {"events_emitted": 0, "clients_lifetime_total": 0, "clients_currently": 0}
        # Server-side event recorder. Set PIPELINE_RECORD_EVENTS=/path/to/file.jsonl
        # to append every emitted event to disk — a complete, hang-proof trace of
        # exactly what the pipeline output, frame by frame. This is the reliable
        # alternative to an SSE HTTP client (which stalls mid-stream on this Pi).
        # Consume with `tail -f`; analyze with tools/annotation_parser + the grade.
        self._record_path = os.environ.get("PIPELINE_RECORD_EVENTS")
        self._record_fh = None
        if self._record_path:
            try:
                self._record_fh = open(self._record_path, "a", buffering=1)  # line-buffered
                log.info("[sse] recording emitted events to %s", self._record_path)
            except Exception as e:
                log.warning("[sse] could not open event recorder %s: %s", self._record_path, e)

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

    def emit(self, camera: str, wall_time_ms: int, tracks: list,
             pts: float = 0.0) -> None:
        """Broadcast a per-frame event to all subscribed clients.

        `pts` is the canonical clock — stream time in seconds, extracted
        from the H.264 bitstream by the capture stage. Clients sync labels
        to the live video by matching this PTS against the
        `requestVideoFrameCallback`-reported PTS of the rendered frame.
        `wall_time_ms` is kept for log/UI human-readable timestamps but
        must NOT be used for sync decisions.
        """
        payload = json.dumps({
            "camera": camera,
            "wall_time_ms": wall_time_ms,
            "pts": float(pts),
            # seq: monotonic — gaps mean the transport dropped events.
            # emit_ms: server wall time at emit — lets the client split
            # transport delay from pipeline delay. Neither is a sync clock;
            # pts remains canonical.
            "seq": self._seq,
            "emit_ms": time.time() * 1000.0,
            "tracks": tracks,
        })
        self._seq += 1
        if self._record_fh is not None:
            try:
                self._record_fh.write(payload + "\n")
            except Exception:
                pass  # never let recording break the live broadcast
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
            self.stats["clients_lifetime_total"] += 1
            self.stats["clients_currently"] += 1

    def _remove_client(self, camera: str, q: "queue.Queue") -> None:
        with self._clients_lock:
            if camera in self._clients and q in self._clients[camera]:
                self._clients[camera].remove(q)
                self.stats["clients_currently"] = max(0, self.stats["clients_currently"] - 1)
