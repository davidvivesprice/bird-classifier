> **HISTORICAL** — This document describes a design, plan, or system state that has been superseded. Kept for decision-trail context.

# Unified Classification Query System — Design Spec

## Problem

The review system has TWO separate query paths for the same data:
- `get_pending_classifications()` — Classify tab (images without reviews)
- `get_reviewed_entries()` — Classified tab (images with reviews)

Plus ad-hoc queries in api.py for counts, species lists, batch operations. Every filtering rule (exclude trash, match corrected species, effective species logic) must be implemented in every query. We've fixed the same filtering bug 8+ times this session in different locations. Each fix patches one spot but leaves others broken.

## Goal

One query function. One set of filtering rules. Every endpoint calls it.

## Design

### Core Function: `get_classifications()`

```python
def get_classifications(status="pending", species=None, verdict=None,
                        camera=None, date=None, offset=0, limit=50):
    """Single query for all classification views.

    Args:
        status: "pending" (no review), "reviewed" (has review, not trash), "all"
        species: Filter by effective species (corrected if corrected, original otherwise)
        verdict: Filter by specific verdict (only applies when status="reviewed")
        camera: Filter by camera name
        date: Filter by source_date
        offset/limit: Pagination

    Returns:
        List of dicts with unified format.
    """
```

### Effective Species Rule (baked in once)

The "effective species" for any image is:
- If `verdict='wrong'` AND `correct_species` is set (not empty, not 'not_a_bird'): use `correct_species`
- Otherwise: use `common_name`

This determines:
1. Which species folder the file lives in
2. What the species filter matches against
3. What the `species` field in the response contains

SQL:
```sql
CASE WHEN r.verdict = 'wrong' AND r.correct_species IS NOT NULL
     AND r.correct_species != '' AND r.correct_species != 'not_a_bird'
THEN r.correct_species
ELSE c.common_name
END AS effective_species
```

### Trash Exclusion Rule (baked in once)

When `status="reviewed"`, always exclude:
- `verdict = 'trash'`
- `verdict = 'wrong' AND correct_species = 'not_a_bird'`

When `status="pending"`, exclude images that have ANY review record.

When `status="all"`, no exclusion.

### Unified Response Format

Every call returns the same dict structure:

```python
{
    "file": "feeder_2026-03-30_10-11-28.jpg",
    "species": "Black-capped Chickadee",        # effective species
    "original_species": "Dark-eyed Junco",       # common_name (what AI said)
    "verdict": "wrong",                          # null if pending
    "correct_species": "Black-capped Chickadee", # null if not corrected
    "confidence": 0.89,
    "source_timestamp": "2026-03-30 10:11:28",
    "camera": "feeder",
    "best_detection_json": "...",
    "review_timestamp": "2026-03-30 14:00:00",   # null if pending
}
```

The frontend always uses `item.species` for display, `item.original_species` for "was: X". No client-side CASE logic.

### Count Function

```python
def count_classifications(status="pending", species=None, verdict=None):
    """Count matching the same filters as get_classifications."""
```

Uses the EXACT same WHERE clause as `get_classifications`. One implementation, no divergence.

### Species List Function

```python
def list_classification_species(status="reviewed"):
    """List distinct effective species names for filter dropdowns."""
```

Returns species from the effective_species column, not raw common_name. Corrected images show under their corrected species.

### Migration Plan

| Current Function | New Call | Endpoint |
|-----------------|----------|----------|
| `get_pending_classifications()` | `get_classifications(status="pending")` | `/api/review/pending` |
| `get_reviewed_entries()` | `get_classifications(status="reviewed")` | `/api/review/classified` |
| Ad-hoc count in `review_classified` | `count_classifications(status="reviewed", species=sp)` | `/api/review/classified` |
| Species list in `review_classified` | `list_classification_species(status="reviewed")` | `/api/review/classified` |
| `smart_queue` pending query | `get_classifications(status="pending")` | `/api/review/smart-queue` |
| `batch` species query | `get_classifications(status="pending", species=sp)` | `/api/review/batch` |

Old functions get a `# DEPRECATED — use get_classifications()` comment and are removed after all callers migrate.

### Verification Gate

After each endpoint migration:
1. Run pytest
2. Take Playwright screenshot of the affected tab
3. Visually verify: correct species shown, no trash, no dead images, counts match
4. Test the species filter with a corrected species
5. Then commit

### Files Modified

| File | Changes |
|------|---------|
| `reviews_db.py` | Add `get_classifications()`, `count_classifications()`, `list_classification_species()`. Deprecate old functions. |
| `dashboard/api.py` | Migrate all review endpoints to call new functions. Remove ad-hoc queries. |
| `dashboard/index.html` | Update response field names if needed (`species` vs `common_name`). |
| `tests/test_classifications_query.py` | New: test unified query with all status/filter combinations. |

### Success Criteria

1. Every species filter matches the effective species (corrected > original)
2. No trashed items appear in any non-trash view
3. Counts always match visible items
4. One function, one set of rules — no duplicate filtering logic
5. Playwright screenshots verify each migrated endpoint visually
