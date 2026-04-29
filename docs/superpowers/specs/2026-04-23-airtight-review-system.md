# Airtight Review System + Gamified UI (2026-04-23)

**Origin:** David's 2026-04-23 brief — "getting a bad model because the system of sorting and value assigning is broken is the stupidest way to fail." Every bird that hits the review queue must have its verdict honored end-to-end. AND the review process must be pleasant enough that David actually wants to do it.

**Scope:** Two tracks, shipped as one feature.
1. **Airtight plumbing** — every verdict flows atomically from UI → DB → filesystem → training set. No lost corrections. No partial states. No ghost rows.
2. **Gamified UI** — a new page at `/review` that turns the queue into a rewarding loop. Keyboard-first, fast, with feedback that tells David when he's helping the model most.

Live mockup: `dashboard/review-ideas.html` (this commit). Paired with this spec so the shape is concrete, not just prose.

---

## Part 1: The airtight plumbing

### Current-state bugs (audited 2026-04-23)

**Bug A — Corrections can be silently overwritten.**
`reviews_db.py:95` uses `INSERT OR REPLACE INTO reviews`. Any subsequent review for the same file wipes the previous row. No history, no detection, no recovery. A UI double-submit, a batch operation, or a careless script clobbers correctly-labeled data.

**Bug B — OFFSET-based pagination loses items under mutation.**
`get_classifications(offset, limit)` in `reviews_db.py:386` queries against a mutating "pending" set. After trashing an item on page 1, the next-page query (higher OFFSET) can skip items the user hasn't seen yet, because each trash shifts the set.

**Bug C — File move and DB update are not atomic.**
`apply_verdict` in `dashboard/api.py:404` calls `_apply_verdict_files` (moves file) then `UPDATE classifications` (marks trashed). If the DB update fails, the file is already in trash but the row still says `action='classified'`. Audit today catches this ex-post (`location_mismatch`) but ex-ante prevention is better.

**Bug D — `_find` only checks `classified/<species>/`.**
`_apply_verdict_files:347` searches `classified_dir.iterdir()` only. If the file happens to be in `annotated/`, `pending/`, or anywhere else, it's not found — DB still gets updated, file stays. Latent data corruption.

**Bug E — No audit log.**
No way to answer "what did David review in the last hour?" or "when was this correction made?" beyond a single `timestamp` column. Makes it impossible to recover from a bug like D.

**Bug F — Annotated mirror not always cleaned.**
Trash path deletes `annotated/<file>`. But the 'wrong' path (`correct_species=X`) moves the classified file but leaves the annotated file stranded as an orphan in the wrong species dir. Over time, annotated/ drifts from reality.

### Design targets (airtight)

| Property | Target |
|---|---|
| Lost corrections | **Impossible** — append-only history, latest wins, prior never deleted |
| Pagination stability | **Keyset, not offset** — `WHERE timestamp < last_seen ORDER BY timestamp DESC LIMIT N` |
| Transactional integrity | **Two-phase commit** — stage file move in txn, rollback on DB-update failure |
| File-location discovery | **Walk all known roots**, not just `classified/<species>/` |
| Auditability | **`review_history` append-only table** — never replaced, queryable by reviewer + date |
| Annotated sync | **Mirror every `apply_verdict` on the annotated/ copy** (delete on trash, move on wrong) |
| Immediate feedback | **Synchronous** — the HTTP response returns after file+DB+history are all committed; UI shows success only then |

### Schema changes

**New table `review_history`:**
```sql
CREATE TABLE review_history (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    file            TEXT    NOT NULL,
    verdict         TEXT    NOT NULL,
    correct_species TEXT    DEFAULT '',
    bird_index      INTEGER DEFAULT 0,
    missed_birds    INTEGER DEFAULT 0,
    reviewer        TEXT    DEFAULT 'dashboard',
    timestamp       TEXT    NOT NULL,
    prev_row_id     INTEGER,  -- fk to prior review of same file; NULL on first
    client_id       TEXT,     -- UI-supplied idempotency key (see "no double-submit" below)
    FOREIGN KEY (prev_row_id) REFERENCES review_history(id)
);
CREATE INDEX idx_rh_file ON review_history(file);
CREATE INDEX idx_rh_timestamp ON review_history(timestamp);
CREATE UNIQUE INDEX idx_rh_client ON review_history(client_id) WHERE client_id IS NOT NULL;
```

**`reviews` table stays** as the denormalized "current state" view — but becomes a MATERIALIZED CACHE of "the most recent `review_history` row per file." The write path:

1. `apply_verdict` receives: `file, verdict, correct_species, client_id`
2. Check `idx_rh_client`: if `client_id` already exists, return the prior response (idempotent retry).
3. Begin transaction.
4. Insert `review_history` row.
5. UPSERT `reviews` row with latest verdict/correct_species (recover-on-restart via the history).
6. Update `classifications.action` accordingly.
7. Commit.
8. Outside txn: execute file moves (with defensive find-everywhere, see Bug D fix).
9. On file-move failure: **do not roll back DB** — instead, enqueue a repair job. The audit (1a) will catch the mismatch and the repair runs hourly.

Rationale for the txn order: DB commit first means we never lose a verdict. Filesystem is eventually consistent with DB via the audit. This avoids "user clicked trash, network blip, file moved but row didn't" → silent data loss.

### Keyset pagination for the review queue

Current:
```python
SELECT ... ORDER BY c.timestamp DESC LIMIT ? OFFSET ?
```

Replace with:
```python
SELECT ... WHERE c.timestamp < ? ORDER BY c.timestamp DESC LIMIT ?
```
where `?` is the timestamp of the last item on the previous page. Client sends it back as an opaque cursor. Trashing an item on page 1 does not shift any item between pages — ordering is stable against deletions.

Edge case: items with identical timestamps → add `c.id` as a tiebreaker in both ORDER BY and the WHERE clause.

### Atomic file operation

New helper `_move_file_safe(filename, dest_dir) → MoveResult`:

```python
def _move_file_safe(filename: str, dest_dir: Path) -> MoveResult:
    """Find file across known roots, move to dest_dir, also move annotated/ mirror."""
    candidates = []
    for root in (CLASSIFIED_DIR, PENDING_DIR, ANNOTATED_DIR):
        for found in root.rglob(filename):
            candidates.append(found)
    if not candidates:
        return MoveResult(ok=False, error="not_found_anywhere")
    # Use the first hit under classified/ preferentially; else first hit anywhere.
    src = next((p for p in candidates if CLASSIFIED_DIR in p.parents), candidates[0])
    dest_dir.mkdir(parents=True, exist_ok=True)
    dst = dest_dir / filename
    if dst.exists():
        return MoveResult(ok=False, error="dest_exists")
    shutil.move(str(src), str(dst))
    # Mirror: annotated copy moves/deletes in lockstep
    ann_src = ANNOTATED_DIR / filename
    if ann_src.exists():
        if dest_dir.name == TRASH_DIR.name:
            ann_src.unlink()  # don't keep trashed birds annotated
        else:
            ann_dst = ANNOTATED_DIR / filename  # annotated location is flat, file stays
            # no-op; annotated is keyed by filename not species dir
    return MoveResult(ok=True, src=src, dst=dst)
```

### No double-submit (idempotency)

Every review API call takes a `client_id` UUID minted by the UI when the card is first presented. Server deduplicates on that key. User double-clicks → second request is a no-op and returns the first response. UI remains responsive (no double-toast, no double-animate).

### Concrete API additions

- `POST /api/review2/{filename}` — new endpoint (keep old one for backward compat, deprecate after migration). Body: `{verdict, correct_species, client_id, last_seen_timestamp}`. Returns: `{ok, history_id, next_cursor}`.
- `GET /api/review2/queue?after=<ts>&limit=N&mode=smart|all` — cursor-based.
- `GET /api/review2/history/{filename}` — audit trail for a specific file. (Implemented as path param, not query param.)
- `POST /api/review2/undo/{history_id}` — reverses a specific history row. Inserts a new history row with `verdict='undone'` and points `prev_row_id` back. The UI's undo button uses this.

### Migration path

1. Ship `review_history` table with trigger-based backfill from existing `reviews`.
2. Dual-write: new API writes to both `reviews` (legacy) and `review_history` (new). Old API still works.
3. Move the /review UI to the new API.
4. After N days, deprecate old `POST /api/review/{filename}` endpoint.
5. Eventually, drop `reviews` in favor of a VIEW that reads the most recent `review_history` per file.

---

## Part 2: Gamified UI proposal

### Principles

- **Keyboard first.** 90% of reviews are one-key: `Y`/`N`/`T`. No mouse. David should feel like he's playing Tetris.
- **One bird at a time.** Full-screen card. Big image. No distractions.
- **Streaks matter.** Consecutive reviews build a streak. Abstain breaks it.
- **Feedback is immediate and physical.** Screen wobble on wrong-click, particle burst on correct, subtle lift on "was confidently wrong" catches.
- **You see what you're helping with.** A small "Species progress" ticker shows how many more confirmed of this species we need for Tier 2 training to stabilize.
- **No drudgery.** Trash is one keystroke, no confirm dialog. Undo exists (Ctrl+Z).
- **Respect the reviewer's time.** Session goals ("15 more to hit today's goal"). Daily streak counter.

### Interaction design (the /review page)

**Main card (full-screen, minimal chrome):**

```
┌─────────────────────────────────────────────────────────┐
│  ≡ Session: 23/50 · Streak: 12 · Rare finds: 2 · ⏱ 4:07 │
├─────────────────────────────────────────────────────────┤
│                                                         │
│                                                         │
│              [  bird image, 90% of card  ]              │
│                                                         │
│                                                         │
│                                                         │
├─────────────────────────────────────────────────────────┤
│  AI says: House Finch (62%)                             │
│                                                         │
│  ┌────────────┐  ┌──────────┐  ┌──────────┐             │
│  │  Y Correct │  │ N Wrong  │  │  T Trash │             │
│  └────────────┘  └──────────┘  └──────────┘             │
│                                                         │
│  Ctrl+Z undo · S skip · ? shortcuts                    │
└─────────────────────────────────────────────────────────┘
```

**Keyboard:**
- `Y` — correct (default path)
- `N` — wrong → panel slides in with species search (fuzzy), pick → commit
- `T` — trash (no confirm)
- `S` — skip (counts against streak)
- `Space` — same as `Y` (ergonomic)
- `Ctrl+Z` — undo last verdict
- `?` — shortcut overlay
- `←` / `→` — reviewed history browser (readonly, stays in queue)

**Gamification layer:**

1. **Streak counter** in the header. Breaks on `S` or `Ctrl+Z`. Small fire emoji at 10+. Bigger fire at 50+.
2. **Rare-find callout** — if the AI said a species with <10 training examples, a subtle "📈 rare training data" chip appears. Reviewing it bumps a "flagship helper" counter.
3. **Catch-the-bot** — when David picks `N` on a high-confidence AI call, a small "✓ you caught a bad call" badge flashes. These are the most valuable reviews.
4. **Session mode selector** — a bar at the top lets David choose the current session's diet:
   - *Anything* (default, mixed queue)
   - *Hairy vs Downy* (Hairy/Downy binary micro-round)
   - *Rare species* (only species with <50 confirmed)
   - *Recently seen* (last 24h)
   - *Multi-bird* (frames with >1 bird, to assign bird_index)
5. **Daily goal** — default 50 reviews. Progress ring. When hit, "Goal!" flash + option to keep going.
6. **Weekly heatmap** — reviews per day as tiny squares. Grows over time, visible at the top.

### Air-tightness UX details

- **No verdict without confirmation of motion.** When David presses `Y`, the card slides out of view with a ~150ms animation. During that time, the verdict is IN FLIGHT. If the request fails (network, DB, etc.), the card slides BACK in with a red toast: "retry?" No silent data loss.
- **Every verdict shows the `client_id`** in the console on dev builds. If same key submitted twice, server no-ops and UI shows "already done."
- **Undo is always an option** for the previous 10 verdicts. Implemented server-side via `review_history`.
- **Session summary at end** (when queue empties or user closes): shows counts per verdict, per species, and any `S` (skipped) items that got re-queued.

### Visual design

- Dark canvas (matches current dashboard theme).
- Huge image. The bird is the hero.
- Only one decision on screen at a time — no grid of thumbnails, no toolbar.
- Accents: soft green for `Y`, muted orange for `N`, desaturated red for `T`. No harsh clinical colors.
- Micro-animations from `chr15m/juice-it` vocabulary: squash on press, screen shake on trash, confetti burst only for streak milestones.

### Mockup file

Built as a live HTML+CSS+JS prototype at `dashboard/review-ideas.html`. Interactive without real data — clicking the verdict buttons shows the animation, demonstrates the undo, and simulates the session streak. No backend calls. Meant to be shown to David and iterated on visually before the real page is built.

---

## Part 3: Implementation progress

### Shipped (2026-04-24)

**DB layer** — commit `a167907`, evolved `reviews_db.py` in-place instead of building a parallel module.

- ✓ `review_history` append-only table with partial unique index on `client_id`.
- ✓ `insert_review()` now writes history first (in a transaction) + updates the `reviews` cache. Idempotent on `client_id`. Returns `{history_id, prev_row_id, duplicate}`.
- ✓ `get_history(file)` — chronological list of all reviews for a file.
- ✓ `undo(history_id, client_id)` — appends `verdict='undone'` entry + restores the `reviews` cache to the prior state.
- ✓ 13 new TDD tests green; 62 existing tests still pass; 419 tests total pass; zero regressions.
- ✓ Backward compat: 3 existing `rdb.insert_review()` callers in `api.py` discard the return value — grep-verified before shipping.

### Shipped (2026-04-24, part 2)

**API layer** — commit `8bf6868`, three endpoints in `dashboard/api.py`:

- ✓ `POST /api/review2/{filename}` — JSON body `{verdict, correct_species?, missed_birds?, bird_index?, client_id?}`. Idempotent via `client_id`. `apply_verdict` runs AFTER DB commit; if the file-move fails, the 1a audit catches the mismatch rather than losing the verdict.
- ✓ `GET /api/review2/history/{filename}` — full chronological audit trail.
- ✓ `POST /api/review2/undo/{history_id}` — body `{client_id?}`. Idempotent.
- ✓ 9 new TDD tests; 428 total tests green; zero regressions.

### Shipped (2026-04-24, part 3)

**Queue endpoint** — commit `ec640a2`:

- ✓ `GET /api/review2/queue` with keyset pagination. Params: `limit`, `after` (cursor = last-seen timestamp), `species`, `camera`. Returns `{items, next_cursor}`. Fetches `limit+1` rows internally to determine has-more without a separate COUNT.
- ✓ 6 new tests. Key invariant covered: trashing an item on page 1 cannot make page 2 skip items.
- ✓ 434 total tests green; zero regressions.

### Shipped (2026-04-25, part 4)

**Integrity audit** — `tools/audit_data_integrity.py` (not `review_system_integrity.py` as originally named). Runs as `com.vives.bird-integrity-audit` LaunchAgent (`--cull` flag). Catches file/DB mismatches the airtight write path prevents going forward.

### Not built

- `_move_file_safe` atomic helper — was planned, not implemented. `apply_verdict()` in `api.py` handles file moves inline.
- `/review-ideas` mockup wired to real API — `dashboard/review-ideas.html` exists as a standalone design mockup only; not connected to live data.
- Part 2 gamified UI (streaks, leaderboard, XP) — not implemented. The review UX shipped as the clean card-grid interface in the main dashboard.

### Deferred

- Gradual cutover of legacy `/api/review/` endpoints — no hurry; they now carry a full audit trail via the evolved `insert_review()`.
- Nightly integrity script (`tools/review_system_integrity.py`).

---

## Part 4: Testing the airtightness

A good sanity script: `tools/review_system_integrity.py` that after a session:

- Every `review_history` row has a matching `classifications` row
- Every `reviews.current` row matches the latest `review_history` for that file
- Every file referenced by a non-trash review exists on disk
- Every `action='trashed:review'` row has its file in trash/
- No classifications row is referenced by a `correct` verdict AND missing from disk
- Idempotency: running the same review twice produces only one `review_history` row

Run nightly. Alerts on any divergence.

---

## Why this matters

David's own words: *"getting a bad model because the system of sorting and value assigning is broken is the stupidest way to fail."*

The 2026-04-08 failure (0/14 accuracy, "everything is Goldfinch") was explicitly traced to contaminated training data — squirrels labeled as Rock Pigeons, empty frames labeled as Flickers, Chickadees labeled as Waxwings. The flagship yard model has 1,673 human-verified samples today that are the bedrock of Tier 2. Losing even ONE of those through a schema race is a direct threat to model quality.

Airtight isn't perfectionism. It's the minimum bar.
