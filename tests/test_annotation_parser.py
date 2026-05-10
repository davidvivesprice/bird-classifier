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
