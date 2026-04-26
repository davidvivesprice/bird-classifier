# 06 · Pi-Review — yes / no per classification

David clicks ✓ or ✗ on Recent Classifications cards. We record the verdict + which classifier produced the row, so per-classifier accuracy stays stable over the system's lifetime. Standalone from the iMac-side `review2` system per the post-split architecture.

## Why this exists, separately

The iMac side has `review2` — a heavyweight verdict system with `correct / wrong / skip / trash / reclassify` verdicts, an audit-trail history table, idempotency via `client_id`, and `apply_verdict` file-move side effects.

Pi-side wants something dumber and faster:

- One verdict per file (latest wins).
- No file moves. The JPG stays where it is so the Live view + Recent strip continue to work.
- Per-classifier accuracy aggregation, captured at click time so it's stable across re-classifications.
- Just enough to answer "how is the active classifier doing" at a glance, and to accumulate ground truth that can later feed Tier 2 training.

## API surface

`dashboard/pi_review.py` defines a FastAPI router at `/api/pi-review`. Mounted by `dashboard/api.py` only when `PI_MODE=1`.

| Method | Path | Body / params | Returns |
|---|---|---|---|
| `POST` | `/api/pi-review/{filename}` | `{verdict: "yes" \| "no"}` | `{ok, file, verdict, model_source}` |
| `DELETE` | `/api/pi-review/{filename}` | — | `{ok, file, deleted}` |
| `GET` | `/api/pi-review/recent` | `?limit=8` (cap 400) | `{items: [{file, source_timestamp, species, confidence, model_source, verdict, reviewed_at}, …]}` |
| `GET` | `/api/pi-review/stats` | — | `{total_reviewed, overall_accuracy, by_model: [{model_source, yes, no, total, accuracy}, …]}` |

All endpoints are PI_MODE-gated; on the iMac the router isn't mounted.

## Storage

`~/bird-snapshots/logs/pi_reviews.db` — SQLite, WAL mode, single table:

```sql
CREATE TABLE pi_reviews (
    file         TEXT PRIMARY KEY,
    verdict      TEXT NOT NULL CHECK (verdict IN ('yes','no')),
    reviewed_at  TEXT NOT NULL,
    model_source TEXT
);
CREATE INDEX idx_pi_reviews_at    ON pi_reviews(reviewed_at);
CREATE INDEX idx_pi_reviews_model ON pi_reviews(model_source);
```

`init_db()` is idempotent and runs at dashboard startup.

`model_source` is captured AT CLICK TIME by reading `extra_json.model_source` from `classifications.db` for the file. The classifier name is therefore frozen even if the file's classification is later re-run with a different model. That's what lets the per-classifier accuracy numbers stay meaningful over time.

## How it shows up in the UI

See `05-dashboard.md` for the layout. Two pieces of the Recent Classifications panel are wired to this:

1. The stats line above the strip:
   `N reviewed · X% correct overall · aiy_onnx: Y% (yes/total) · resnet50_hailo: Z% (yes/total)`
2. Each card's ✓ / ✗ buttons. Click → POST → optimistic UI update + green/red-tinted border per verdict. Click an already-active verdict to clear.

## What this enables

- **At-a-glance accuracy.** "Is the active classifier actually right?" is now answerable without spelunking through `classifications.db`.
- **Per-classifier comparison.** When David switches to `resnet50_hailo` for a Lab session, future verdicts on those rows attribute to ResNet, not AIY. After Tier 2 ships, switching to `flagship_v1` and accumulating verdicts will give us an apples-to-apples accuracy story without juggling separate review tools.
- **Tier 2 ground-truth dataset.** Verdicts are file-keyed; a Tier 2 retrain step can join `pi_reviews.yes` with `classifications.db` to build a "known good per species" set, and `pi_reviews.no` to flag confused cases.

## What this is NOT

- Not a corrector. There's no "if no, what was it really?" capture. (The iMac `review2` has that for `verdict=wrong, correct_species=...`.) When we need that, it'll be a separate field; right now it would slow the click-to-mark cadence David wants for burning through a backlog.
- Not append-only. The latest verdict overwrites prior. No history table.
- Not connected to file moves, retention, or DB cleanup. Verdicts are pure metadata.

## Code references

- `dashboard/pi_review.py` — the whole module (~180 lines).
- `dashboard/api.py:90+` — PI_MODE-gated mount.
- `dashboard/pi_dash.html:loadRecent` / `loadReviewStats` — the JS that drives the strip + stats line.
- `pi_dash.html` `.recent-btn`, `.recent-card.reviewed.verdict-{yes,no}` — the per-theme CSS.
