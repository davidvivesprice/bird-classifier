import pytest
from tools.annotation_parser import parse_timecode, parse_annotations, Visit

def test_parse_timecode_full():
    assert parse_timecode("01:23:45:15", fps=30) == pytest.approx(1*3600 + 23*60 + 45 + 15/30)

def test_parse_timecode_truncated_mm_ss():
    # David's "25:25" means 25 min 25 sec
    assert parse_timecode("25:25", fps=30) == pytest.approx(25*60 + 25)

def test_parse_timecode_truncated_mm_ss_ff():
    # David's "25:11" with frames could be ambiguous - we treat as MM:SS
    assert parse_timecode("25:25:11", fps=30) == pytest.approx(25*60 + 25 + 11/30)

def test_parse_timecode_with_inline_comment():
    # David: "1:44:09 (head only)" -> strip comment
    assert parse_timecode("1:44:09 (head only)", fps=30) == pytest.approx(1*60 + 44 + 9/30)

def test_parse_timecode_blank():
    assert parse_timecode("", fps=30) is None
    assert parse_timecode("   ", fps=30) is None

def test_parse_annotations_minimal():
    md = """
### Visit 01
- first_in_frame: 00:00:01
- first_identifiable: 00:00:17
- last_identifiable: 02:07:21
- last_in_frame: 02:07:27
- species: House Finch
- count: 1
- motion_pattern: perched
- notes: long visit
"""
    visits = parse_annotations(md, fps=30)
    assert len(visits) == 1
    v = visits[0]
    assert v.id == "01"
    assert v.first_in_frame_s == pytest.approx(1/30)
    assert v.species == "house finch"
    assert v.motion_pattern == "perched"

def test_parse_annotations_blank_identifiable():
    md = """
### Visit 03
- first_in_frame: 00:22:01
- first_identifiable:
- last_identifiable:
- last_in_frame: 00:25:11
- species: american goldfinch (male)
- motion_pattern: partial
- notes: never fully visible
"""
    visits = parse_annotations(md, fps=30)
    assert len(visits) == 1
    assert visits[0].first_identifiable_s is None
    assert visits[0].last_identifiable_s is None
    # Species with parenthetical → keep as-is (lowercased)
    assert visits[0].species == "american goldfinch (male)"

def test_parse_annotations_multiple_first_identifiable():
    # David's Visit 06 has TWO first_identifiable lines
    md = """
### Visit 06
- first_in_frame: 1:43:16
- first_identifiable: 1:44:09 (head only)
- first_identifiable: 1:50:06 (full but still partial)
- last_identifiable: 2:04:27
- last_in_frame: 2:04:29
- species: american goldfinch (female)
"""
    visits = parse_annotations(md, fps=30)
    assert len(visits) == 1
    # First wins; second goes into a parser-warning list
    assert visits[0].first_identifiable_s == pytest.approx(1*60 + 44 + 9/30)
    assert "duplicate first_identifiable" in visits[0].parser_warnings[0]

def test_parse_annotations_all_empty_block_skipped():
    md = """
### Visit 99
- first_in_frame:
- first_identifiable:
- last_identifiable:
- last_in_frame:
- species:
"""
    visits = parse_annotations(md, fps=30)
    assert len(visits) == 0


def test_blank_then_real_first_identifiable_captures_real():
    # Regression for the I1 bug: blank first occurrence used to swallow
    # the later real value via the saw_first_id_already flag.
    # Use unambiguous MM:SS form so the timecode value isn't affected by
    # the MM:SS:FF vs HH:MM:SS heuristic (see I3 in the review notes).
    md = """
### Visit 01
- first_in_frame: 00:01
- first_identifiable:
- first_identifiable: 00:10
- last_identifiable: 00:20
- last_in_frame: 00:25
- species: House Finch
"""
    visits = parse_annotations(md, fps=30)
    assert len(visits) == 1
    assert visits[0].first_identifiable_s == pytest.approx(10.0)
    # No "duplicate" warning emitted (the blank line shouldn't count)
    assert not any("duplicate first_identifiable" in w for w in visits[0].parser_warnings)


def test_count_unparseable_falls_back_with_warning():
    md = """
### Visit 01
- first_in_frame: 00:00:01
- last_in_frame: 00:00:05
- species: House Finch
- count: two
"""
    visits = parse_annotations(md, fps=30)
    assert len(visits) == 1
    assert visits[0].count == 1
    assert any("unparseable count" in w for w in visits[0].parser_warnings)


def test_load_annotations_file_roundtrip(tmp_path):
    # End-to-end via load_annotations_file: write a tempfile, read it back.
    from tools.annotation_parser import load_annotations_file
    md = """
### Visit 01
- first_in_frame: 25:25
- first_identifiable: 26:21
- last_identifiable: 32:25
- last_in_frame: 33:00
- species: American Goldfinch (male)
- motion_pattern: perched and hopping
- notes: first identifiable is questionable
"""
    p = tmp_path / "annot.md"
    p.write_text(md)
    visits = load_annotations_file(p, fps=30)
    assert len(visits) == 1
    v = visits[0]
    # 25:25 = 25min 25sec = 1525s
    assert v.first_in_frame_s == pytest.approx(25 * 60 + 25)
    assert v.species == "american goldfinch (male)"
    assert v.motion_pattern == "perched and hopping"
    assert v.notes == "first identifiable is questionable"
