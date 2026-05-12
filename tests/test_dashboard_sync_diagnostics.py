import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def test_sync_diag_exposes_video_frame_clock():
    html = (ROOT / "dashboard" / "pi_dash.html").read_text()

    assert "requestVideoFrameCallback" in html
    assert "videoFrameHz" in html
    assert "lastVideoMediaTime" in html
    assert "clockDeltaMs" in html
    assert "eventAgeMsRough" in html
    assert "get sync" in html

