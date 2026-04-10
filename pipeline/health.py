"""HealthState — shared pipeline health dict + status computation + HTTP endpoint."""
from __future__ import annotations
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Optional


class HealthState:
    def __init__(self):
        self._lock = threading.Lock()
        self._data: dict = {"pipeline": {}, "shared": {}}
        self._start_time = time.time()

    def update(self, camera: str, component: str, stats: dict):
        with self._lock:
            cam = self._data["pipeline"].setdefault(camera, {})
            cam[component] = dict(stats)

    def update_shared(self, component: str, stats: dict):
        with self._lock:
            self._data["shared"][component] = dict(stats)

    def snapshot(self) -> dict:
        with self._lock:
            data = {
                "pipeline": {
                    cam: {comp: dict(s) for comp, s in comps.items()}
                    for cam, comps in self._data["pipeline"].items()
                },
                "shared": {k: dict(v) for k, v in self._data["shared"].items()},
                "uptime_s": int(time.time() - self._start_time),
            }
        data["overall"] = self._compute_status(data)
        return data

    def _compute_status(self, data: dict) -> str:
        """Roll-up: ok / degraded / broken."""
        order = {"ok": 0, "degraded": 1, "broken": 2}
        worst = "ok"
        for cam, comps in data.get("pipeline", {}).items():
            cap = comps.get("capture", {})
            fps = cap.get("fps")
            age_ms = cap.get("last_frame_age_ms")
            if age_ms is not None and age_ms > 60_000:
                return "broken"
            if fps is not None and fps < 3:
                return "broken"
            if fps is not None and fps < 4.5:
                if order[worst] < order["degraded"]:
                    worst = "degraded"
            detector = comps.get("detector", {})
            p99 = detector.get("yolo_ms_p99")
            if p99 is not None and p99 > 150:
                if order[worst] < order["degraded"]:
                    worst = "degraded"
            classifier = comps.get("classifier", {})
            if classifier.get("lock_timeouts", 0) > 10:
                if order[worst] < order["degraded"]:
                    worst = "degraded"
        return worst


class HealthServer:
    """Minimal HTTP server that exposes /api/pipeline/health as JSON."""
    def __init__(self, health: HealthState, port: int = 8100):
        self.health = health
        self.port = port
        self._server: Optional[ThreadingHTTPServer] = None
        self._thread: Optional[threading.Thread] = None

    def start(self):
        health = self.health

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, fmt, *args):
                pass  # silence access log

            def do_GET(self):
                if self.path.startswith("/api/pipeline/health"):
                    body = json.dumps(health.snapshot()).encode("utf-8")
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Access-Control-Allow-Origin", "*")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                else:
                    self.send_response(404)
                    self.end_headers()

        self._server = ThreadingHTTPServer(("0.0.0.0", self.port), Handler)
        self._thread = threading.Thread(
            target=self._server.serve_forever, name="health-server", daemon=True
        )
        self._thread.start()

    def stop(self):
        if self._server:
            try:
                self._server.shutdown()
            except Exception:
                pass
