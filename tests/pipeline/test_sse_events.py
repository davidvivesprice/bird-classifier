"""Tests for pipeline/sse_events.py — HTTP SSE server for track events."""
import json
import socket
import threading
import time
import urllib.request


def _pick_port():
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def test_sse_server_starts_and_accepts_connections():
    from pipeline.sse_events import SSEEventServer
    port = _pick_port()
    server = SSEEventServer(port=port)
    server.start()
    try:
        resp = urllib.request.urlopen(
            f"http://127.0.0.1:{port}/health", timeout=2
        )
        assert resp.status == 200
    finally:
        server.stop()


def test_sse_server_emits_events_to_subscribers():
    from pipeline.sse_events import SSEEventServer
    port = _pick_port()
    server = SSEEventServer(port=port)
    server.start()
    try:
        received = []

        def subscribe():
            try:
                resp = urllib.request.urlopen(
                    f"http://127.0.0.1:{port}/events/sse?camera=feeder", timeout=5
                )
                buf = b""
                start = time.time()
                while time.time() - start < 3:
                    chunk = resp.read1(1024)
                    if not chunk:
                        break
                    buf += chunk
                    if b"\n\n" in buf:
                        break
                for line in buf.decode("utf-8").split("\n"):
                    if line.startswith("data: "):
                        received.append(json.loads(line[6:]))
                        break
            except Exception as e:
                pass

        t = threading.Thread(target=subscribe, daemon=True)
        t.start()
        # Give the subscriber time to connect before emitting
        time.sleep(0.5)
        server.emit("feeder", 1_700_000_000_000, [
            {"track_id": 1, "bbox": [100, 100, 200, 200], "species": "Test Bird",
             "species_confidence": 0.9, "model_source": "yard", "is_locked": False,
             "frame_count": 1, "bbox_center_x": 150, "frame_width": 640, "frame_height": 360}
        ])
        t.join(timeout=5)
        assert len(received) >= 1, f"no events received"
        assert received[0]["camera"] == "feeder"
        assert received[0]["tracks"][0]["species"] == "Test Bird"
    finally:
        server.stop()


def test_sse_server_filters_by_camera():
    from pipeline.sse_events import SSEEventServer
    port = _pick_port()
    server = SSEEventServer(port=port)
    server.start()
    try:
        received = []

        def subscribe():
            try:
                resp = urllib.request.urlopen(
                    f"http://127.0.0.1:{port}/events/sse?camera=feeder", timeout=5
                )
                buf = b""
                start = time.time()
                while time.time() - start < 2:
                    chunk = resp.read1(1024)
                    if chunk:
                        buf += chunk
                for line in buf.decode("utf-8").split("\n"):
                    if line.startswith("data: "):
                        received.append(json.loads(line[6:]))
            except Exception:
                pass

        t = threading.Thread(target=subscribe, daemon=True)
        t.start()
        time.sleep(0.5)
        # Emit for ground first — feeder subscriber must NOT receive it
        server.emit("ground", 1_700_000_000_000, [{"track_id": 99}])
        time.sleep(0.1)
        # Emit for feeder — subscriber must receive
        server.emit("feeder", 1_700_000_000_001, [{"track_id": 42}])
        t.join(timeout=3)
        assert len(received) >= 1, "expected at least one event for feeder"
        for ev in received:
            assert ev["camera"] == "feeder", f"leak: received {ev['camera']} event on feeder channel"
    finally:
        server.stop()


def test_sse_server_missing_camera_query_returns_400():
    from pipeline.sse_events import SSEEventServer
    port = _pick_port()
    server = SSEEventServer(port=port)
    server.start()
    try:
        import urllib.error
        try:
            urllib.request.urlopen(
                f"http://127.0.0.1:{port}/events/sse", timeout=2
            )
            assert False, "expected HTTPError 400"
        except urllib.error.HTTPError as e:
            assert e.code == 400
    finally:
        server.stop()
