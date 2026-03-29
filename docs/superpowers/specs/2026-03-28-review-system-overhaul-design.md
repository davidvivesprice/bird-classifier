# Review System Overhaul + Dashboard Fixes — Design Spec

## Problem

The review system has 17 bugs tracing to one root cause: no single function enforces "verdict → file location + DB state." Every endpoint reinvents file movement (or doesn't). Additionally, several dashboard UX issues and dead NAS code need fixing.

## Scope

- **Part A**: Review system — single `apply_verdict()` function, all endpoints use it
- **Part B**: Audio health check — fix "no detections" when detections exist
- **Part C**: Nighttime detection — verify correct (believed working as designed)
- **Part D**: Species activity selector — searchable dropdown + random species on load
- **Part E**: Unknown food rate — fix inflated per-hour calculation
- **Part F**: Remove all NAS code — NAS decommissioned (192.168.5.92), nothing should reference it
- **Part G**: Activity heatmap label — shows both audio + camera data, needs to say so

---

## Part A: Review System Overhaul

### Core Fix: `apply_verdict()`

One function in `dashboard/api.py`. Every review endpoint calls it.

```python
def apply_verdict(filename, verdict, correct_species=""):
    """Move file + update DB to match verdict. Single source of truth.

    Returns {"moved": bool, "from_dir": str|None, "to_dir": str|None, "error": str|None}
    """
```

#### Rules

| Verdict | Classified file | Annotated file | classifications.common_name |
|---------|----------------|----------------|---------------------------|
| correct | stays in classified/{species}/ | stays in annotated/ | no change |
| wrong + correct_species | move to classified/{correct_species}/ | stays in annotated/ | UPDATE to correct_species |
| wrong + "not_a_bird" | move to trash/ | move to trash/ | unchanged |
| trash | move to trash/ | move to trash/ | unchanged |
| skip | move to skipped/ | stays in annotated/ | unchanged |
| reclassify (missed) | stays put | stays | unchanged |

#### Implementation Details

1. Find classified file by searching all subdirs of `classified/` (same as existing `_find_classified()`)
2. Move it to the target directory
3. If verdict is "wrong" with `correct_species` (not "not_a_bird"):
   - `UPDATE classifications SET common_name = ? WHERE file = ?`
4. If verdict is "trash" or "not_a_bird":
   - Also move annotated copy to `trash/` (same filename, no `cls_` prefix)
5. Return result dict (never raise, never silently fail)

#### Callers — Every Review Endpoint

| Endpoint | Current behavior | Fix |
|----------|-----------------|-----|
| `submit_review` (api.py:1297) | Partial file moves, no DB update | Call `apply_verdict()` |
| `batch_confirm` (api.py:932) | No file moves at all | Call `apply_verdict()` per file |
| `batch_reject` (api.py:962) | No file moves at all | Call `apply_verdict()` per file |
| `bulk_reclassify` (api.py:648) | No file moves at all | Call `apply_verdict()` per file |
| `update_review` (api.py:1404) | No file moves at all | Call `apply_verdict()` with new verdict |

### Header Count Fix

`classifications_db.py` `count_classified()` — subtract trashed/not_a_bird:

```sql
SELECT COUNT(*) FROM classifications c
WHERE c.action = 'classified'
AND NOT EXISTS (
    SELECT 1 FROM reviews r WHERE r.file = c.file
    AND (r.verdict = 'trash' OR (r.verdict = 'wrong' AND r.correct_species = 'not_a_bird'))
)
```

### Verdict Label Display Fix

`index.html:5403` — the Classified tab verdict label mapping:

Current (broken): anything not "correct" or "wrong" shows as "Missed Birds"
Fixed: map each verdict to its actual name (correct, wrong, trashed, skipped, missed birds)

Already partially fixed in this session — verify it's complete.

### Startup Consistency Check

Add to `classify.py` `main()`, before entering watch mode:

```python
def _cleanup_orphan_records():
    """Delete DB records pointing to files that no longer exist on disk."""
    # Query all classified records, check if file exists, delete orphans
    # Log count removed
```

Runs once at startup, takes seconds.

---

## Part B: Audio Health Check

**Verified:** `_check_audio_analyzer_health()` (api.py:261-284) queries the BirdNET DB correctly. The DB has 879 detections today. The function augments metrics with `detections_today` and `last_detection`.

**Possible issues:**
1. `_get_birdnet_conn()` may return a cached/stale connection in WAL mode
2. The metrics endpoint at `localhost:8098/metrics` may be down (audio analyzer pauses at night)
3. When `metrics["status"]` is not "ok", the detail string isn't set — dashboard may show stale text

**Fix:**
- Ensure `_get_birdnet_conn()` uses `check_same_thread=False` and opens fresh
- When audio analyzer is paused (nighttime), still show today's detection count from DB
- Always set the detail string, even when metrics endpoint is unreachable

---

## Part C: Nighttime Detection

**Verified working as designed.** `solar_utils.py` correctly calculates sunset for Martha's Vineyard (41.35, -70.74). Sunset today ~19:02 EDT + 30 min offset = nighttime at 19:32. Classifier sleeps 300s between checks during night. Audio analyzer fully pauses.

No code change. If David sees unexpected behavior, check specific log timestamps.

---

## Part D: Species Activity Selector

**Current:** `<input type="text">` with `<datalist>` autocomplete (index.html:2443-2482). Opens blank — user must type and search. Previous chart data persists if you switch tabs and come back.

**Replace with custom combobox:**

```html
<div class="combo-select" id="activity-species-combo">
    <input type="text" placeholder="Select a species..." autocomplete="off">
    <div class="combo-dropdown">
        <!-- populated dynamically, sorted by detection count -->
        <div class="combo-option" data-value="Song Sparrow">Song Sparrow (1,234)</div>
        ...
    </div>
</div>
```

Behavior:
- **Click input or arrow**: Opens full scrollable list (sorted by count, most common first)
- **Type**: Filters list in real time
- **Click option or press Enter**: Selects species, loads activity data
- **Escape or click outside**: Closes dropdown
- **Tab open**: Auto-select random species from top 10, load its data immediately

CSS: dark theme, matches existing dashboard style. Max-height dropdown with scroll.

---

## Part E: Unknown Food Rate

**Bug location:** api.py:2670 — `food_hours.get(food, 1)` defaults "unknown" to 1 hour, inflating rate.

**Fix:** Exclude "unknown" from food preferences entirely:

```python
for food, count in by_food.items():
    if food == "unknown":
        continue  # Can't calculate rate without food log data
    ...
```

If ALL food is "unknown" (no food log entries), show: "No food data logged" instead of the food preferences section.

---

## Part F: Remove All NAS Code

NAS (VivesSyn, 192.168.5.92) is decommissioned. Remove all references.

**Production code to fix:**

| File | Line(s) | What to remove/fix |
|------|---------|-------------------|
| `dashboard/api.py:61` | Comment mentioning NAS nginx proxy | Remove |
| `dashboard/api.py:288-302` | `_check_go2rtc()` error says "NAS unreachable" | Change to "go2rtc unreachable" |
| `dashboard/index.html:2058` | NAS status dot `<span id="sdot-nas">` | Remove entirely |
| `dashboard/index.html:2162` | "Camera feeds require NAS proxy" hint | Change to "Camera feeds require local network access" |
| `dashboard/index.html:5999` | `['api','cams','audio','nas']` status dots array | Remove 'nas' |
| `dashboard/index.html:6041-6042` | `_setDot('nas', svc.nas)` | Remove |
| `dashboard/index.html:6094` | `{key: 'nas', name: 'NAS Proxy', icon: '🌐'}` | Remove |
| `live_detector.py:10,110,291` | Comments mentioning NAS proxy | Update to say "local go2rtc" |

**Docs (old references, non-critical):**
- `GUIDE.md` has NAS IPs and SSH commands — update to note NAS is decommissioned
- Old specs/plans in `docs/superpowers/` — leave as historical record, no action needed

**Health monitor:** No NAS references found — clean.

---

## Part G: Activity Heatmap Label

**Verified:** `/api/activity/heatmap` (api.py:2811-2883) queries BOTH:
- BirdNET audio DB (`notes` table) — lines 2818-2843
- Camera classifications DB — lines 2847-2872

Results are merged into one heatmap.

**Fix:** Add subtitle in the HTML: "All detections (camera + audio) by hour — last 7 days"

---

## Files to Modify

| File | Changes |
|------|---------|
| `dashboard/api.py` | `apply_verdict()`, fix all 5 review endpoints, fix audio health detail, remove NAS text, fix food rate |
| `dashboard/index.html` | Combobox selector, random species on load, heatmap label, remove NAS dot/references, fix header count, fix verdict labels |
| `classify.py` | Startup orphan cleanup |
| `classifications_db.py` | Add `update_common_name(file, species)`, adjust `count_classified()` |
| `live_detector.py` | Remove NAS comments (3 lines) |
| `GUIDE.md` | Note NAS decommissioned |

## Success Criteria

1. Every review verdict moves files to the correct location
2. `classifications.common_name` updated when species is corrected
3. Batch and bulk operations move files same as single review
4. No orphan DB records at startup
5. Header count excludes trashed/not_a_bird
6. Audio health shows detection count even when analyzer paused
7. Species activity: searchable dropdown, random species on load, no blank state
8. Zero NAS references in production code
9. Heatmap labeled "camera + audio"
10. "Unknown" food excluded from rate calculations
