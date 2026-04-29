# Review UI Shared Helpers

**Audience:** future engineers / Claude touching the Review tab in `dashboard/index.html`.

## What

Two JS helpers in `dashboard/index.html` that all Review-tab sub-tabs use for
verdict-handling and pagination:

- `applyVerdictToUI(file, verdict, correctSpecies) → bool` — single source of
  truth for what a verdict looks like in the UI. Returns `true` if a card was
  removed from the DOM (caller may want to refetch when the grid empties).
  - `trash` / `wrong` → animate out (CSS `verdict-removed` keyframe) + remove
    from DOM after 240ms
  - `correct` → green "Confirmed" badge, card stays
  - `skip` → gray "Skipped" badge, card stays
  - `reclassify` → amber "Re-queued" badge, card stays

  Selector strategy: matches `[data-file="<file>"]` first; falls back to
  `.skipped-grid .skipped-card` with `data.file` or `innerHTML` substring
  match for grids that haven't been migrated to data-file yet.

- `loadQueue(state, renderFn)` + `queueNextPage(state, renderFn)` +
  `queuePrevPage(state, renderFn)` + `recordVerdictOnQueue(state, removed)`
  — shared pagination that tracks verdicts-since-last-fetch and adjusts
  `offset` on Next-page so items pulled up from the next page are not
  skipped. Forwards arbitrary `state.params` as URL query params.

## Why

Before this refactor, each sub-tab (Classify, Classified, Skipped, Missed,
Batch) had its own offset variable + its own verdict-UI behavior. Trash on
the Classified tab grayed out the card; trash on the Lightbox auto-advanced;
trash on Batch removed from selection. Inconsistent. Pagination after several
verdicts could skip rows because `offset` didn't account for the shrunken
result set.

Audit findings (2026-04-25) made it worse: ~30% of saved rows are classifier
noise. David needs to plow through cleanup with consistent UX, and every
"row got lost in pagination" silently corrupts the cleanup pass.

## How to add a new paginated review surface

```javascript
var myQueueState = {
  endpoint: '/bird-api/my-queue',
  pageSize: 24,
  offset: 0,
  verdictsSinceFetch: 0,
  params: { species: '', verdict: '', camera: '', multibird: '' },
  lastResp: null,
};

function _renderMyGrid(resp) { /* build HTML, update page-info + buttons */ }

function loadMyItems() {
  return loadQueue(myQueueState, _renderMyGrid);
}
function loadMyPage(dir) {
  if (dir === 'next') queueNextPage(myQueueState, _renderMyGrid);
  else if (dir === 'prev') queuePrevPage(myQueueState, _renderMyGrid);
}

// In the verdict click handler:
async function onVerdict(file, verdict, correctSpecies) {
  await reviewSubmit2(file, { verdict: verdict, correct_species: correctSpecies });
  var removed = applyVerdictToUI(file, verdict, correctSpecies);
  recordVerdictOnQueue(myQueueState, removed);
}
```

For the rendered cards to be reachable by `applyVerdictToUI`, set
`data-file="<file>"` on each card root.

## Multibird filter (server-side)

`reviews_db.get_classifications` / `count_classifications` accept `multibird`
as tri-state:

| Value             | SQL clause                                                 |
|-------------------|------------------------------------------------------------|
| `"exclude"`       | `birds_json IS NULL OR json_array_length(birds_json) <= 1` |
| `"only"` / truthy | `json_array_length(birds_json) > 1`                        |
| `""` / `None`     | (no filter)                                                |

Both `/api/review/pending` and `/api/review/classified` accept `multibird`
and `camera` query params with these semantics. UI dropdowns on Classify and
Classified sub-tabs map directly to these values.

## Files

- Helper definitions: `dashboard/index.html`
  - `applyVerdictToUI` ~line 3768
  - `_appendBadge` ~3792, `_updateGridCard` alias ~3809, `applyVerdictAndRecord` ~3817
  - `verdict-removed` CSS keyframe ~1054
  - `loadQueue` / `queueNextPage` / `queuePrevPage` / `recordVerdictOnQueue`
    ~3354–3395
- Server filters: `reviews_db.py` `_build_classification_query` (multibird
  tri-state); `dashboard/api.py` `review_pending` + `review_classified`
- Current callers:
  - Classified — `loadClassifiedItems` (`classifiedQueueState`)
  - Skipped — `loadSkippedFrames`
  - Missed — `loadMissedBirds`
  - Batch — `loadBatchReview` + batch confirm/reject
  - Classify (one-at-a-time) — `submitReview`
  - Lightbox — `lightboxReview`

## Migration history

Implemented 2026-04-25 per
`docs/superpowers/plans/2026-04-25-review-ui-shared-helpers.md`.

Replaces the previous per-tab `_updateGridCard()` (kept as a thin alias for
backward compatibility).

Commits: `c9c4bca` `e3a4c13` `3352948` `652859f` `50aaf38` `6ff2fb7`
`dd75b45` `fb31adb` `d4820c1` `b4f9a80` `2ebf8ce`.
