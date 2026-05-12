import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "dashboard"))


def test_pipeline_events_ws_forwards_sse_payloads(monkeypatch):
    from starlette.testclient import TestClient
    import dashboard.api as api

    async def fake_payloads(camera):
        assert camera == "feeder"
        yield '{"camera":"feeder","tracks":[{"track_id":7}]}'

    monkeypatch.setattr(api, "_iter_pipeline_sse_payloads", fake_payloads, raising=False)

    client = TestClient(api.app, raise_server_exceptions=False)
    with client.websocket_connect("/api/pipeline/events/ws?camera=feeder") as ws:
        assert ws.receive_text() == '{"camera":"feeder","tracks":[{"track_id":7}]}'


def test_sse_data_parser_handles_chunk_boundaries():
    import dashboard.api as api

    events, tail = api._extract_sse_data_events("data: {\"a\":")
    assert events == []
    assert tail == "data: {\"a\":"

    events, tail = api._extract_sse_data_events(tail + "1}\n\n: keepalive\n\ndata: {\"b\":2}\n\n")
    assert events == ['{"a":1}', '{"b":2}']
    assert tail == ""

