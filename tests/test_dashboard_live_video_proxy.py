import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "dashboard"))


def test_pi_dashboard_uses_same_origin_video_proxy():
    html = (ROOT / "dashboard" / "pi_dash.html").read_text()

    assert "go2rtc.vivessato.com" not in html
    assert "script.src = '/video-stream.js';" in html
    assert "video.src = `${go2rtcWs}/api/ws?src=${next}`;" in html
    assert "`video: ${videoDiag()}\\n`" in html


def test_dashboard_serves_video_stream_wrapper():
    from starlette.testclient import TestClient
    import dashboard.api as api

    client = TestClient(api.app, raise_server_exceptions=False)
    response = client.get("/video-stream.js")

    assert response.status_code == 200
    assert "customElements.define('video-stream'" in response.text
    assert "from './video-rtc.js'" in response.text


def test_demo_stream_is_allowed_through_go2rtc_proxy():
    import dashboard.api as api

    assert "feeder-demo" in api.ALLOWED_STREAMS
