"""DebugStream — MJPEG-over-WebSocket broadcast server."""
from __future__ import annotations
import logging
import threading
import time
from typing import Optional

log = logging.getLogger(__name__)


class ClientState:
    def __init__(self, websocket, camera: str):
        self.websocket = websocket
        self.camera = camera
        self.failed = False
        self.last_send_ms = time.time() * 1000

    def send(self, data: bytes):
        self.websocket.send(data)
        self.last_send_ms = time.time() * 1000

    def mark_failed(self):
        self.failed = True


class DebugStream:
    def __init__(self, port: int = 8101):
        self.port = port
        self.clients: dict = {"feeder": [], "ground": []}
        self._lock = threading.Lock()
        self.latest_frame: dict = {}
        self.stats = {
            "active_clients": 0, "frames_sent": 0,
            "dropped_clients": 0, "start_time": time.time(),
        }
        self._server = None
        self._serve_thread: Optional[threading.Thread] = None
        self._stop = threading.Event()

    def start(self):
        self._stop.clear()
        self._serve_thread = threading.Thread(
            target=self._serve, name="debug-stream-serve", daemon=True
        )
        self._serve_thread.start()

    def stop(self):
        self._stop.set()
        if self._server is not None:
            try:
                self._server.shutdown()
            except Exception:
                pass

    def _serve(self):
        try:
            from websockets.sync.server import serve
        except ImportError:
            log.error("websockets library not installed")
            return
        try:
            with serve(self._handle_client, "0.0.0.0", self.port) as server:
                self._server = server
                server.serve_forever()
        except Exception as e:
            log.error("Debug stream server error: %s", e)

    def _handle_client(self, websocket):
        try:
            path = websocket.request.path
        except Exception:
            try:
                websocket.close(1002, "No path")
            except Exception:
                pass
            return

        if "/feeder" in path:
            camera = "feeder"
        elif "/ground" in path:
            camera = "ground"
        else:
            try:
                websocket.close(1002, "Unknown camera")
            except Exception:
                pass
            return

        client = ClientState(websocket, camera)
        with self._lock:
            self.clients.setdefault(camera, []).append(client)
            self.stats["active_clients"] = sum(len(v) for v in self.clients.values())

        # Send poster frame immediately if we have one
        poster = self.latest_frame.get(camera)
        if poster:
            try:
                websocket.send(poster)
            except Exception:
                pass

        try:
            for _ in websocket:
                pass  # drain pings from client
        except Exception:
            pass
        finally:
            with self._lock:
                try:
                    self.clients[camera].remove(client)
                except (ValueError, KeyError):
                    pass
                self.stats["active_clients"] = sum(len(v) for v in self.clients.values())

    def push(self, camera: str, jpeg_bytes: bytes, frame_time_ms: float):
        """Called by annotator threads. Broadcasts JPEG to all clients for this camera."""
        self.latest_frame[camera] = jpeg_bytes
        with self._lock:
            clients = list(self.clients.get(camera, []))
        for client in clients:
            if client.failed:
                continue
            try:
                client.send(jpeg_bytes)
                self.stats["frames_sent"] += 1
            except Exception:
                client.mark_failed()
                self.stats["dropped_clients"] += 1
