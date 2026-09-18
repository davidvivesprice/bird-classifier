"""UniFi Protect event frames → normalised rows in `unifi_events`.

Frame shapes are the ones captured from Protect 7.2.105 on 2026-09-18 with a
passive subscriber: an `add` opens the event (no `end`), an `update` carries
the same item with `end` filled in. The camera lives in `item.device`.
"""
import json
import sqlite3

from pipeline.unifi_events import (
    CAMERA_NAMES,
    FEEDER_CAMERA_ID,
    UnifiEventStore,
    parse_message,
)

OTHER_CAMERA_ID = "6a6a3fda0126dd03e40267e8"  # "Shed Garage"

ADD = {
    "type": "add",
    "item": {
        "id": "2a882aff-26ef-4688-b284-ae702863cd51",
        "modelKey": "event",
        "type": "motion",
        "start": 1789743270712,
        "device": FEEDER_CAMERA_ID,
    },
}
UPDATE = {
    "type": "update",
    "item": {
        "id": "2a882aff-26ef-4688-b284-ae702863cd51",
        "type": "motion",
        "start": 1789743270712,
        "end": 1789743278212,
        "device": FEEDER_CAMERA_ID,
        "modelKey": "event",
    },
}


def _rows(store):
    cur = store.conn.execute(
        "SELECT id, camera, type, start_ms, end_ms, smart_types, raw_json, updated_ms "
        "FROM unifi_events ORDER BY start_ms")
    cols = [c[0] for c in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


# ── parse_message ───────────────────────────────────────────────────────────

def test_parse_add_motion_frame():
    ev = parse_message(ADD)
    assert ev["action"] == "add"
    assert ev["id"] == ADD["item"]["id"]
    assert ev["device"] == FEEDER_CAMERA_ID
    assert ev["type"] == "motion"
    assert ev["start_ms"] == 1789743270712
    assert ev["end_ms"] is None
    assert ev["smart_types"] is None
    assert ev["item"] == ADD["item"]


def test_parse_update_carries_end():
    ev = parse_message(UPDATE)
    assert ev["action"] == "update"
    assert ev["end_ms"] == 1789743278212


def test_parse_smart_detect_types_list():
    msg = json.loads(json.dumps(ADD))
    msg["item"]["type"] = "smartDetectZone"
    msg["item"]["smartDetectTypes"] = ["animal"]
    ev = parse_message(msg)
    assert ev["type"] == "smartDetectZone"
    assert ev["smart_types"] == ["animal"]


def test_parse_accepts_json_text():
    ev = parse_message(json.dumps(ADD))
    assert ev is not None and ev["id"] == ADD["item"]["id"]


def test_parse_rejects_non_event_frames():
    assert parse_message({"type": "update", "item": {"modelKey": "camera", "id": "x"}}) is None
    assert parse_message({"type": "add", "item": {"modelKey": "event"}}) is None  # no id
    assert parse_message({"hello": 1}) is None
    assert parse_message("not json {") is None
    assert parse_message(42) is None


def test_parse_tolerates_missing_device_and_start():
    ev = parse_message({"type": "update", "item": {"id": "abc", "modelKey": "event", "end": 5}})
    assert ev == {
        "action": "update", "id": "abc", "device": None, "type": None,
        "start_ms": None, "end_ms": 5, "smart_types": None,
        "item": {"id": "abc", "modelKey": "event", "end": 5},
    }


# ── UnifiEventStore ─────────────────────────────────────────────────────────

def test_store_add_then_update_is_one_row(tmp_path):
    store = UnifiEventStore(str(tmp_path / "pipeline.db"))
    assert store.apply(parse_message(ADD), now_ms=1000) is True
    rows = _rows(store)
    assert len(rows) == 1
    assert rows[0]["camera"] == "feeder"
    assert rows[0]["end_ms"] is None
    assert rows[0]["updated_ms"] == 1000

    assert store.apply(parse_message(UPDATE), now_ms=2000) is True
    rows = _rows(store)
    assert len(rows) == 1
    assert rows[0]["end_ms"] == 1789743278212
    assert rows[0]["start_ms"] == 1789743270712
    assert rows[0]["updated_ms"] == 2000
    assert json.loads(rows[0]["raw_json"])["end"] == 1789743278212
    store.close()


def test_store_update_never_clears_a_known_end(tmp_path):
    store = UnifiEventStore(str(tmp_path / "pipeline.db"))
    store.apply(parse_message(UPDATE))
    store.apply(parse_message(ADD))  # late/duplicate open frame, no `end`
    assert _rows(store)[0]["end_ms"] == 1789743278212
    store.close()


def test_store_ignores_other_cameras(tmp_path):
    store = UnifiEventStore(str(tmp_path / "pipeline.db"))
    msg = json.loads(json.dumps(ADD))
    msg["item"]["device"] = OTHER_CAMERA_ID
    assert store.apply(parse_message(msg)) is False
    assert _rows(store) == []
    store.close()


def test_store_camera_map_is_configurable(tmp_path):
    store = UnifiEventStore(str(tmp_path / "pipeline.db"),
                            cameras={OTHER_CAMERA_ID: "shed"})
    msg = json.loads(json.dumps(ADD))
    msg["item"]["device"] = OTHER_CAMERA_ID
    assert store.apply(parse_message(msg)) is True
    assert _rows(store)[0]["camera"] == "shed"
    assert store.apply(parse_message(ADD)) is False  # feeder not in this map
    store.close()


def test_store_partial_update_without_device_touches_known_row_only(tmp_path):
    store = UnifiEventStore(str(tmp_path / "pipeline.db"))
    store.apply(parse_message(ADD))
    partial = {"type": "update", "item": {"id": ADD["item"]["id"], "modelKey": "event", "end": 1789743279000}}
    assert store.apply(parse_message(partial)) is True
    assert _rows(store)[0]["end_ms"] == 1789743279000

    unknown = {"type": "update", "item": {"id": "never-seen", "modelKey": "event", "end": 7}}
    assert store.apply(parse_message(unknown)) is False
    assert len(_rows(store)) == 1
    store.close()


def test_store_smart_types_stored_as_json(tmp_path):
    store = UnifiEventStore(str(tmp_path / "pipeline.db"))
    msg = json.loads(json.dumps(ADD))
    msg["item"]["type"] = "smartDetectZone"
    msg["item"]["smartDetectTypes"] = ["animal", "person"]
    store.apply(parse_message(msg))
    row = _rows(store)[0]
    assert row["type"] == "smartDetectZone"
    assert json.loads(row["smart_types"]) == ["animal", "person"]
    store.close()


def test_store_prune_drops_old_rows(tmp_path):
    store = UnifiEventStore(str(tmp_path / "pipeline.db"))
    old = json.loads(json.dumps(UPDATE))
    old["item"]["id"] = "old"
    old["item"]["start"] = 1000
    old["item"]["end"] = 2000
    store.apply(parse_message(old))
    store.apply(parse_message(UPDATE))
    assert store.prune(older_than_ms=10_000) == 1
    assert [r["id"] for r in _rows(store)] == [UPDATE["item"]["id"]]
    store.close()


def test_store_is_wal_and_coexists_with_pipeline_tracks(tmp_path):
    db = tmp_path / "pipeline.db"
    other = sqlite3.connect(str(db))
    other.execute("CREATE TABLE pipeline_tracks (track_id INTEGER PRIMARY KEY, camera TEXT, "
                  "start_time INTEGER, end_time INTEGER)")
    other.commit()

    store = UnifiEventStore(str(db))
    assert store.conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert store.conn.execute("PRAGMA busy_timeout").fetchone()[0] >= 5000
    store.apply(parse_message(ADD))

    names = {r[0] for r in other.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"pipeline_tracks", "unifi_events"} <= names
    assert other.execute("SELECT COUNT(*) FROM unifi_events").fetchone()[0] == 1
    other.close()
    store.close()


def test_feeder_id_matches_refresh_rtsp_camera_table():
    assert CAMERA_NAMES[FEEDER_CAMERA_ID] == "feeder"
    assert FEEDER_CAMERA_ID == "690e999401027503e400043b"
