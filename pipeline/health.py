"""HealthState — shared pipeline health dict + status computation + HTTP endpoint."""
from __future__ import annotations
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Optional

try:
    from solar_utils import is_nighttime
except Exception:
    def is_nighttime():
        return False


class HealthState:
    def __init__(self):
        self._lock = threading.Lock()
        self._data: dict = {"pipeline": {}, "shared": {}}
        self._start_time = time.time()
        # Latest debug frames: per-camera JPEG bytes with YOLO boxes drawn.
        # Written by CameraProcessThread, served by HealthServer GET /debug/latest.jpg.
        self.latest_debug_jpeg: dict = {}  # camera_name → JPEG bytes

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
        """Compute overall health status from per-camera metrics.

        Precedence: broken > degraded > ok. Worst state wins.

        Rules (matches docs/superpowers/specs/2026-04-11-live-detection-v3-design.md §6):
        - broken:
            * any camera capture.last_frame_age_ms > 60000 during daytime
            * any camera capture.ffmpeg_restarts_last_hour > 10
        - degraded:
            * any camera detector.yolo_ms_p99 (when not None) > 1000
            * any camera with (dropped_oldest / max(frames_processed, 1)) > 0.05
            * any camera classifier.lock_timeouts > 5
        - ok: none of the above
        """
        worst = "ok"
        night = is_nighttime()

        for _cam, comps in data.get("pipeline", {}).items():
            capture = comps.get("capture", {})
            detector = comps.get("detector", {})
            classifier = comps.get("classifier", {})

            # BROKEN checks — short-circuit to broken immediately
            if not night:
                frame_age = capture.get("last_frame_age_ms")
                if frame_age is not None and frame_age > 60_000:
                    return "broken"

            restart_storm = capture.get("ffmpeg_restarts_last_hour", 0)
            if restart_storm > 10:
                return "broken"

            # DEGRADED checks (accumulate; only escalate if not already broken)
            if worst == "broken":
                continue

            yolo_p99 = detector.get("yolo_ms_p99")
            if yolo_p99 is not None and yolo_p99 > 1000:
                worst = "degraded"

            frames = max(capture.get("frames_processed", 0), 1)
            dropped = capture.get("dropped_oldest", 0)
            if dropped / frames > 0.05:
                worst = "degraded"

            if classifier.get("lock_timeouts", 0) > 5:
                worst = "degraded"

        return worst


class HealthServer:
    """HTTP server: /api/pipeline/health (JSON) + /debug/latest.jpg (annotated frame)."""
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

            def do_GET(inner_self):
                if inner_self.path.startswith("/api/pipeline/health"):
                    body = json.dumps(health.snapshot()).encode("utf-8")
                    inner_self.send_response(200)
                    inner_self.send_header("Content-Type", "application/json")
                    inner_self.send_header("Access-Control-Allow-Origin", "*")
                    inner_self.send_header("Content-Length", str(len(body)))
                    inner_self.end_headers()
                    inner_self.wfile.write(body)
                elif inner_self.path.startswith("/debug/latest.jpg"):
                    from urllib.parse import urlparse, parse_qs
                    qs = parse_qs(urlparse(inner_self.path).query)
                    cam = qs.get("camera", ["feeder"])[0]
                    jpeg = health.latest_debug_jpeg.get(cam)
                    if jpeg:
                        inner_self.send_response(200)
                        inner_self.send_header("Content-Type", "image/jpeg")
                        inner_self.send_header("Access-Control-Allow-Origin", "*")
                        inner_self.send_header("Cache-Control", "no-cache")
                        inner_self.send_header("Content-Length", str(len(jpeg)))
                        inner_self.end_headers()
                        inner_self.wfile.write(jpeg)
                    else:
                        inner_self.send_response(204)
                        inner_self.end_headers()
                else:
                    inner_self.send_response(404)
                    inner_self.end_headers()

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
