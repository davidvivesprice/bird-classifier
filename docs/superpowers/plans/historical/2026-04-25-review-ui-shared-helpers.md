> **HISTORICAL** — This document describes a design, plan, or system state that has been superseded. Kept for decision-trail context.

# Review UI Shared Helpers Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Extract two shared JS helpers (`applyVerdictToUI` + `loadQueue`) and apply them across all Review-tab sub-tabs so verdict behavior + pagination behavior are uniform, and "trash"/"wrong" verdicts immediately remove cards from the visible queue (no more gray-out).

**Architecture:** `dashboard/index.html` currently has `_updateGridCard()` defined once but only called from 2 of 6 verdict sites; remaining sites do their own UI mutation, leading to inconsistency (gray-out vs. auto-advance vs. badge). Pagination uses `offset+limit` across all 6 queue UIs, vulnerable to row-shift mutations between page-turns. Replace with: (a) one verdict-UI handler that animates trash/wrong out of the DOM and badges correct/skip, (b) one queue-load helper that refetches the same page after each verdict and only advances offset on explicit page-turns by `(PAGE_SIZE - verdicts_since_fetch)`. Migrate all sub-tabs to use both.

**Tech Stack:** Vanilla JS in `dashboard/index.html` (no build step, no JS test runner). Python pytest + Playwright (already in `venv-coral`) for integration tests against the live dashboard at `localhost:8099`.

---

## Pre-task: Pre-read these locations to understand current state

- `dashboard/index.html:3253-3265` — `reviewSubmit2()` (the API helper, already shared)
- `dashboard/index.html:3650-3673` — `_updateGridCard()` (the existing partial UI helper)
- `dashboard/index.html:2486-2604` — Review sub-tab structure (Classify, Batch, Skipped, Classified, Missed, Manage)
- `dashboard/index.html:5701-5712` — `switchReviewSubtab()` and which loaders fire on each subtab
- `dashboard/index.html:5725-5795` — Skipped sub-tab loader + pagination
- `dashboard/index.html:5796-5837` — Missed sub-tab loader
- `dashboard/index.html:5847-5942` — Classified sub-tab loader + pagination
- `dashboard/api.py:2196-2230` — `/api/review2/queue` (keyset pagination, already exists; we'll use it)
- `dashboard/api.py:2499-2528` — `/api/review/classified` (offset pagination — what Classified tab uses today)

The existing `apiFetch()` (`dashboard/index.html:3267+`) handles HTTP; don't replace it.

---

## Task 1: Extract `applyVerdictToUI` helper (replaces `_updateGridCard`)

**Files:**
- Modify: `dashboard/index.html:3650-3673` (the `_updateGridCard` function)
- Add CSS keyframe near existing `.review-*` styles (line ~1050)

- [ ] **Step 1.1: Add CSS keyframe + class for slide-out animation**

Find existing review-related CSS around line 1050 (`.btn-trash`, `.btn-skip`, etc.). Add right after:

```css
  @keyframes verdict-removed {
    0%   { opacity: 1; transform: translateY(0) scale(1); }
    100% { opacity: 0; transform: translateY(-12px) scale(0.92); }
  }
  .verdict-removed {
    animation: verdict-removed 0.22s ease-in forwards;
    pointer-events: none;
  }
```

- [ ] **Step 1.2: Replace `_updateGridCard` body with new helper**

Find `dashboard/index.html:3650`. Replace the entire function (lines 3650-3673) with:

```javascript
  // Shared UI handler for what happens to a card after a verdict lands.
  // Behavior is uniform across all review-tab grids:
  //   trash, wrong → animate out + remove from DOM (gone, not grayed-out)
  //                  Server moves the file (apply_verdict): trash→trash/,
  //                  wrong+species→classified/<species>/. UI just removes
  //                  from current view; refetch will reflect the new state.
  //   correct       → green "Confirmed" badge, card stays
  //   skip          → small "Skipped" badge, card stays
  //   reclassify    → amber "Re-queued" badge, card stays
  //   anything else → no visual change
  //
  // Returns true if the card was removed from DOM (caller may want to
  // decrement a counter / refetch when the grid empties).
  function applyVerdictToUI(file, verdict, correctSpecies) {
    var cards = document.querySelectorAll('[data-file="' + file + '"]');
    var removed = false;
    cards.forEach(function(card) {
      if (verdict === 'trash' || verdict === 'wrong') {
        // Remove with a short animation, then prune from DOM.
        card.classList.add('verdict-removed');
        removed = true;
        setTimeout(function() {
          if (card.parentNode) card.parentNode.removeChild(card);
        }, 240);  // matches keyframe duration + small buffer
      } else if (verdict === 'correct') {
        _appendBadge(card, 'Confirmed', '#065f46', '#6ee7b7');
      } else if (verdict === 'skip') {
        _appendBadge(card, 'Skipped', '#374151', '#d1d5db');
      } else if (verdict === 'reclassify') {
        _appendBadge(card, 'Re-queued', '#78350f', '#fcd34d');
      }
    });
    return removed;
  }

  function _appendBadge(card, text, bg, fg) {
    // Idempotent: don't double-badge if user clicks twice.
    if (card.querySelector('.verdict-badge')) return;
    var badge = document.createElement('div');
    badge.className = 'verdict-badge';
    badge.style.cssText = 'position:absolute;top:4px;right:4px;background:'
      + bg + ';color:' + fg + ';padding:2px 6px;border-radius:4px;'
      + 'font-size:0.7rem;z-index:1;';
    badge.textContent = text;
    if (getComputedStyle(card).position === 'static') {
      card.style.position = 'relative';
    }
    card.appendChild(badge);
  }

  // Keep the old name as a thin alias so existing call sites that haven't
  // migrated yet still work. New code should call applyVerdictToUI directly.
  function _updateGridCard(file, verdict, correctSpecies) {
    return applyVerdictToUI(file, verdict, correctSpecies);
  }
```

- [ ] **Step 1.3: Verify no callers of `_updateGridCard` are broken**

```bash
cd /Users/vives/bird-classifier
grep -n '_updateGridCard\b' dashboard/index.html
```

Expected: 3 matches — the function definition (alias), and 2 call sites at ~3743 and ~3792 (the lightbox paths). All should still work because the alias preserves signature.

- [ ] **Step 1.4: Manual smoke-test in browser**

```bash
launchctl kickstart -k "gui/$(id -u)/com.vives.bird-dashboard"
```

Then in browser at https://birds.vivessato.com/ (hard-refresh with Cmd+Shift+R):
1. Open the Review tab → Classified sub-tab
2. Find any card, click 🗑️ Trash
3. Expected: card animates out (slide up + fade) over ~220ms and is removed from DOM
4. Click 👁️ Confirmed on a different card
5. Expected: green "Confirmed" badge appears in top-right corner

If both work, proceed. If not, examine browser console for errors.

- [ ] **Step 1.5: Commit**

```bash
cd /Users/vives/bird-classifier
git add dashboard/index.html
git commit -m "feat(review): applyVerdictToUI helper — trash/wrong remove from DOM, not gray-out

Replaces _updateGridCard's gray-out behavior with animate-out + DOM removal
for trash and wrong verdicts. Server-side apply_verdict already moves the
file (trash to trash/; wrong+species to classified/<species>/), so the UI
just needs to drop the card from the current view; the refetch will reflect
the new state.

Correct/skip/reclassify still get a small badge (visible but no removal).
_updateGridCard kept as a thin alias so the 2 existing call sites
(lightbox paths) work unchanged. New code should call applyVerdictToUI
directly."
```

---

## Task 2: Migrate Classified sub-tab cards to set `data-file` + use shared selector

**Files:**
- Modify: `dashboard/index.html` Classified sub-tab card rendering (find via `grep -n 'classified-grid' dashboard/index.html`)

- [ ] **Step 2.1: Find the Classified card rendering function**

```bash
cd /Users/vives/bird-classifier
grep -n -B1 -A2 'classified-grid' dashboard/index.html | head -30
```

Locate the function that renders each card into `#classified-grid` (likely named `renderClassifiedItems` or inline after `loadClassifiedItems`).

- [ ] **Step 2.2: Verify each card has `data-file="<filename>"` attribute**

The new `applyVerdictToUI` selects `[data-file="<file>"]`. Read the card-rendering code; if cards don't have `data-file` set, add it.

If you find rendering like:
```javascript
'<div class="skipped-card" onclick="...">'
```
Change to:
```javascript
'<div class="skipped-card" data-file="' + escAttr(item.file) + '" onclick="...">'
```

If `data-file` is already present, no change needed.

- [ ] **Step 2.3: Reload dashboard + smoke-test**

Hard-refresh, navigate to Classified, click trash on a card. Confirm card disappears (not gray-out).

- [ ] **Step 2.4: Commit (only if changes were needed)**

```bash
cd /Users/vives/bird-classifier
git add dashboard/index.html
git commit -m "feat(review): Classified cards expose data-file for applyVerdictToUI selector"
```

If no changes were needed (cards already had `data-file`), skip this commit.

---

## Task 3: Wire Classified sub-tab verdict actions to `applyVerdictToUI`

**Files:**
- Modify: `dashboard/index.html` — locations where Classified-tab cards trigger verdicts (search for `onclick=.*lightboxReview\|onclick=.*submitReview` near the classified-grid rendering)

- [ ] **Step 3.1: Identify the Classified-tab verdict trigger paths**

```bash
cd /Users/vives/bird-classifier
grep -n -B2 -A4 'classified-grid\|skipped-card' dashboard/index.html | grep -E 'onclick|lightboxReview|submitReview' | head -10
```

Most Classified-tab cards likely open the lightbox; the lightbox's verdict buttons (`lightboxReview('trash')`, etc.) call `_updateGridCard` (now an alias for `applyVerdictToUI`). So Classified→lightbox→verdict path already works.

- [ ] **Step 3.2: If there are inline verdict buttons on Classified cards, route them through `applyVerdictToUI`**

If the Classified grid has direct verdict buttons (e.g., a per-card 🗑️ that calls something other than `_updateGridCard`), find those click handlers and ensure they call `applyVerdictToUI(file, verdict, correctSpecies)` after `await reviewSubmit2(...)`.

If there are no inline buttons (all verdict actions go through the lightbox), no code change needed.

- [ ] **Step 3.3: Manual end-to-end on Classified**

In browser:
1. Open Classified sub-tab
2. Click a card to open lightbox
3. Click Trash button in lightbox
4. Expected: lightbox closes (or advances if multi-detection); the card in the underlying Classified grid animates out + disappears

- [ ] **Step 3.4: Commit (only if changes needed)**

```bash
cd /Users/vives/bird-classifier
git add dashboard/index.html
git commit -m "feat(review): Classified-tab inline verdict buttons route through applyVerdictToUI"
```

Skip if no changes were needed.

---

## Task 4: Extract `loadQueue` pagination helper

**Files:**
- Modify: `dashboard/index.html` — add new helper near `apiFetch` definition (around line 3267+)

- [ ] **Step 4.1: Add the loadQueue helper**

Find `apiFetch` definition in `dashboard/index.html` (around line 3267). Immediately after the `apiFetch` function's closing brace, add:

```javascript
  // Shared queue/pagination handler. Used by Skipped, Classified, Missed,
  // Batch, and any future paginated review grid.
  //
  // Why this exists: each grid had its own offset+limit logic. When a user
  // applied verdicts (trash/wrong/etc.) and items left the result set on
  // the server, advancing offset by PAGE_SIZE skipped rows that pulled up
  // from the next page. This helper tracks "how many verdicts since last
  // fetch" and adjusts offset accordingly when the user clicks Next.
  //
  // state shape: { endpoint, pageSize, offset, verdictsSinceFetch, params }
  // Returns: a state object with methods (refetch, nextPage, prevPage,
  // recordVerdict). Caller provides a renderFn to draw the items.
  function loadQueue(state, renderFn) {
    var params = Object.assign({}, state.params || {}, {
      limit: state.pageSize,
      offset: state.offset,
    });
    var qs = Object.keys(params)
      .filter(function(k) { return params[k] !== '' && params[k] != null; })
      .map(function(k) { return encodeURIComponent(k) + '=' + encodeURIComponent(params[k]); })
      .join('&');
    var url = state.endpoint + (qs ? '?' + qs : '');
    return apiFetch(url).then(function(resp) {
      state.verdictsSinceFetch = 0;
      state.lastResp = resp;
      if (typeof renderFn === 'function') renderFn(resp);
      return resp;
    });
  }

  // Call this from the verdict-handler whenever a card is removed via
  // applyVerdictToUI. The caller (per-tab) tracks its own state object.
  function recordVerdictOnQueue(state, removed) {
    if (removed) state.verdictsSinceFetch = (state.verdictsSinceFetch || 0) + 1;
  }

  // Advance to the next page. Adjusts offset by (pageSize - verdictsSinceFetch)
  // so items that pulled up from the next page when verdicts were applied
  // are not skipped.
  function queueNextPage(state, renderFn) {
    var advance = state.pageSize - (state.verdictsSinceFetch || 0);
    if (advance < 1) advance = 1;  // always advance at least one
    state.offset += advance;
    return loadQueue(state, renderFn);
  }

  // Go back one full page (verdicts on previous pages are already reflected
  // server-side; the simple offset rewind is correct for "Newer" navigation).
  function queuePrevPage(state, renderFn) {
    state.offset = Math.max(0, state.offset - state.pageSize);
    return loadQueue(state, renderFn);
  }
```

- [ ] **Step 4.2: Verify no syntax errors in the served JS**

```bash
cd /Users/vives/bird-classifier
launchctl kickstart -k "gui/$(id -u)/com.vives.bird-dashboard" && sleep 4
curl -sS http://localhost:8099/ -o /tmp/served.html
node -c /tmp/served.html 2>&1 | head -5  # rough syntax check; ignore HTML wrapper warnings
```

If `node` not available: open the dashboard in a browser, open DevTools console, look for "SyntaxError" on the page-load. If the page loads at all, the JS parsed successfully.

- [ ] **Step 4.3: Commit**

```bash
cd /Users/vives/bird-classifier
git add dashboard/index.html
git commit -m "feat(review): loadQueue/queueNextPage/queuePrevPage shared pagination

Replaces per-tab offset+limit logic with a shared state-object pattern that
tracks verdicts-since-last-fetch and adjusts offset on Next-page so items
pulled up from the next page (after server-side verdict filtering) are not
skipped. Recovers from the offset-shift bug that loses rows in pagination."
```

---

## Task 5: Migrate Classified sub-tab loader to `loadQueue`

**Files:**
- Modify: `dashboard/index.html:5847-5942` — `loadClassifiedItems`, `loadClassifiedPage`, related state vars

- [ ] **Step 5.1: Replace per-tab pagination state with a state object**

Find lines 5847-5850 (the `var classifiedItems / classifiedTotal / classifiedOffset / CLASSIFIED_PAGE_SIZE` declarations). Replace with:

```javascript
  var classifiedQueueState = {
    endpoint: '/bird-api/review/classified',
    pageSize: 24,
    offset: 0,
    verdictsSinceFetch: 0,
    params: { species: '', verdict: '' },
    lastResp: null,
  };
  var classifiedLoaded = false;
```

- [ ] **Step 5.2: Replace `loadClassifiedItems` to use loadQueue**

Find `window.loadClassifiedItems = async function(resetOffset)` (line ~5852). Replace the whole function body up to but not including `window.loadClassifiedPage` with:

```javascript
  window.loadClassifiedItems = function(resetOffset) {
    if (resetOffset !== false) classifiedQueueState.offset = 0;
    classifiedQueueState.params.species =
      document.getElementById('classified-species-filter').value || '';
    classifiedQueueState.params.verdict =
      document.getElementById('classified-verdict-filter').value || '';
    var grid = document.getElementById('classified-grid');
    grid.innerHTML = '<div class="loading-msg">Loading classified items...</div>';
    return loadQueue(classifiedQueueState, function(resp) {
      classifiedLoaded = true;
      _renderClassifiedGrid(resp);
    }).catch(function(e) {
      grid.innerHTML = '<div class="loading-msg" style="color:var(--danger)">Error: '
        + (e && e.message ? e.message : 'load failed') + '</div>';
    });
  };
```

- [ ] **Step 5.3: Extract the existing render code into `_renderClassifiedGrid(resp)`**

Find the part of the old `loadClassifiedItems` that builds the grid HTML from `resp.items`. Extract it into a new helper:

```javascript
  function _renderClassifiedGrid(resp) {
    var items = resp.items || [];
    var total = resp.total || 0;
    var grid = document.getElementById('classified-grid');
    // ... copy the existing grid-building HTML logic from old loadClassifiedItems here ...
    // Make sure each card sets data-file="<file>" on its outer div.
    // Also update the page-info span:
    document.getElementById('classified-page-info').textContent =
      (classifiedQueueState.offset + 1) + '–'
      + Math.min(classifiedQueueState.offset + items.length, total)
      + ' of ' + total;
    // Update prev/next button disabled state:
    document.getElementById('classified-prev-btn').disabled = classifiedQueueState.offset === 0;
    document.getElementById('classified-next-btn').disabled =
      (classifiedQueueState.offset + items.length) >= total;
  }
```

The existing rendering code in lines ~5866-5935 (or wherever the HTML-building happens) goes inside this helper. **Do not change the visual output** — just relocate it.

- [ ] **Step 5.4: Replace `loadClassifiedPage` to use queueNextPage/queuePrevPage**

Find `window.loadClassifiedPage = function(dir)` (line ~5936). Replace with:

```javascript
  window.loadClassifiedPage = function(dir) {
    if (dir === 'next') {
      queueNextPage(classifiedQueueState, _renderClassifiedGrid);
    } else if (dir === 'prev') {
      queuePrevPage(classifiedQueueState, _renderClassifiedGrid);
    }
  };
```

- [ ] **Step 5.5: Wire `applyVerdictToUI` to `recordVerdictOnQueue`**

This is the link that makes pagination stable. We need verdict actions on Classified cards to record into `classifiedQueueState`.

The cleanest place: where the verdict POST returns. Likely in `lightboxReview` or `submitReview` or wherever a Classified-card's verdict is submitted. After the existing `applyVerdictToUI` call, add (where `removed` is the return value):

```javascript
      var removed = applyVerdictToUI(file, verdict, correctSpecies);
      if (typeof classifiedQueueState !== 'undefined') {
        recordVerdictOnQueue(classifiedQueueState, removed);
      }
```

If multiple verdict-call sites apply, do this for each.

- [ ] **Step 5.6: Hard-refresh + manual end-to-end**

```bash
launchctl kickstart -k "gui/$(id -u)/com.vives.bird-dashboard" && sleep 4
```

In browser:
1. Open Review tab → Classified sub-tab
2. Click trash on 3 visible cards (each animates out)
3. Click `Older →`
4. Expected: page advances, NEXT 24 items appear (no items skipped — the 3 trashed earlier didn't shift the offset incorrectly)
5. Click `← Newer`
6. Expected: returns to first page (now showing 21 items if 3 were trashed and the server-side query excludes them)

- [ ] **Step 5.7: Commit**

```bash
cd /Users/vives/bird-classifier
git add dashboard/index.html
git commit -m "feat(review): Classified sub-tab uses loadQueue + applyVerdictToUI

Removes the per-tab offset variable (classifiedOffset) in favor of a state
object (classifiedQueueState) consumed by loadQueue. Verdict actions now
flow through applyVerdictToUI (animate-out for trash/wrong) and inform the
queue state via recordVerdictOnQueue, so Next-page advances correctly even
after multiple verdicts on the current page."
```

---

## Task 6: Migrate Skipped sub-tab to shared helpers

**Files:**
- Modify: `dashboard/index.html:5719-5795` — `loadSkippedFrames`, `loadSkippedPage`, related state vars

- [ ] **Step 6.1: Replace per-tab state vars (lines 5719-5723)**

Replace:
```javascript
  var skippedLoaded = false;
  var skippedFiles = [];
  var skippedTotal = 0;
  var skippedOffset = 0;
  var SKIPPED_PAGE_SIZE = 24;
```

With:
```javascript
  var skippedLoaded = false;
  var skippedQueueState = {
    endpoint: '/bird-api/skipped',
    pageSize: 24,
    offset: 0,
    verdictsSinceFetch: 0,
    params: {},
    lastResp: null,
  };
```

- [ ] **Step 6.2: Replace `loadSkippedFrames` body**

Replace the body of `async function loadSkippedFrames()` (line ~5725) with:

```javascript
  async function loadSkippedFrames() {
    var grid = document.getElementById('skipped-grid');
    grid.innerHTML = '<div class="loading-msg">Loading skipped frames...</div>';
    return loadQueue(skippedQueueState, function(resp) {
      skippedLoaded = true;
      _renderSkippedGrid(resp);
    }).catch(function(e) {
      grid.innerHTML = '<div class="loading-msg" style="color:var(--danger)">Error: '
        + (e && e.message ? e.message : 'load failed') + '</div>';
    });
  }
```

- [ ] **Step 6.3: Extract Skipped rendering into `_renderSkippedGrid`**

Same pattern as Task 5.3 — move the HTML-building logic from the old `loadSkippedFrames` into a `_renderSkippedGrid(resp)` helper. Update page-info + prev/next button states from `skippedQueueState`. Make sure each card has `data-file=` attribute.

- [ ] **Step 6.4: Replace `loadSkippedPage`**

Replace:
```javascript
  window.loadSkippedPage = function(dir) {
    // ...old offset math...
  };
```

With:
```javascript
  window.loadSkippedPage = function(dir) {
    if (dir === 'next') queueNextPage(skippedQueueState, _renderSkippedGrid);
    else if (dir === 'prev') queuePrevPage(skippedQueueState, _renderSkippedGrid);
  };
```

- [ ] **Step 6.5: Wire applyVerdictToUI → recordVerdictOnQueue for Skipped cards**

Per Task 5.5 pattern. Wherever a Skipped-tab card triggers a verdict, after `applyVerdictToUI`, add:

```javascript
      if (typeof skippedQueueState !== 'undefined') {
        recordVerdictOnQueue(skippedQueueState, removed);
      }
```

- [ ] **Step 6.6: Hard-refresh + manual end-to-end on Skipped**

Same shape as Task 5.6 but in the Skipped sub-tab.

- [ ] **Step 6.7: Commit**

```bash
cd /Users/vives/bird-classifier
git add dashboard/index.html
git commit -m "feat(review): Skipped sub-tab uses loadQueue + applyVerdictToUI"
```

---

## Task 7: Migrate Missed sub-tab to shared helpers

**Files:**
- Modify: `dashboard/index.html:5796-5837` — `loadMissedBirds` and friends

The Missed sub-tab fetches from `/bird-api/review/missed`. It doesn't have explicit pagination buttons today (probably loads everything in one go), but it does have verdict actions. So Task 7 is simpler — just route verdicts through `applyVerdictToUI`.

- [ ] **Step 7.1: Audit `loadMissedBirds`**

```bash
cd /Users/vives/bird-classifier
sed -n '5796,5840p' dashboard/index.html
```

If it does have pagination → follow Task 6 pattern with a `missedQueueState`. If not → just ensure `data-file=` is on cards and verdict actions call `applyVerdictToUI`.

- [ ] **Step 7.2: Apply minimal changes**

If pagination exists: follow Task 6 pattern.
If not: ensure cards have `data-file="<file>"` and verdict click handlers call `applyVerdictToUI(file, verdict, correctSpecies)` after `await reviewSubmit2(...)`.

- [ ] **Step 7.3: Hard-refresh + manual end-to-end on Missed**

Open Missed sub-tab. Trash a card. Expected: animate-out + remove. Re-open Missed (or click Rerun-Missed and reopen) — expected: card stays gone.

- [ ] **Step 7.4: Commit**

```bash
cd /Users/vives/bird-classifier
git add dashboard/index.html
git commit -m "feat(review): Missed sub-tab routes verdicts through applyVerdictToUI"
```

---

## Task 8: Migrate Batch sub-tab to shared helpers

**Files:**
- Modify: `dashboard/index.html` — `loadBatchReview`, `batchConfirmSelected`, `batchRejectSelected`

The Batch sub-tab is a multi-select grid where you select N cards then bulk-confirm or bulk-reject. The verdict-UI behavior should match: confirmed cards get the "Confirmed" badge, rejected cards animate out.

- [ ] **Step 8.1: Find batch-flow code**

```bash
cd /Users/vives/bird-classifier
grep -n -B1 -A4 'batchConfirmSelected\|batchRejectSelected\|loadBatchReview\|_batchSelected' dashboard/index.html | head -40
```

- [ ] **Step 8.2: Wire `applyVerdictToUI` into batch flow**

In `batchConfirmSelected()` (after the successful POST to `/bird-api/review2/batch-confirm`), iterate the selected files and call `applyVerdictToUI(file, 'correct')` for each.

In `batchRejectSelected()` (or wherever the wrong-batch happens), iterate and call `applyVerdictToUI(file, 'wrong', correctSpecies)` for each.

- [ ] **Step 8.3: Hard-refresh + manual end-to-end on Batch**

Open Batch sub-tab. Select 3 cards. Click batch confirm. Expected: 3 cards get green badges. Select 2 different cards. Click batch reject (with corrected species). Expected: 2 cards animate out + remove.

- [ ] **Step 8.4: Commit**

```bash
cd /Users/vives/bird-classifier
git add dashboard/index.html
git commit -m "feat(review): Batch sub-tab uses applyVerdictToUI for confirm/reject feedback"
```

---

## Task 9: Migrate Classify (one-at-a-time) sub-tab + Lightbox

**Files:**
- Modify: `dashboard/index.html` — `submitReview`, `lightboxReview`, `submitWrongCorrection`, `submitLightboxWrong`, `useAsCorrection`

The Classify sub-tab is the one-at-a-time review (different from "Classified" — confusing naming, but yes). It auto-advances via `reviewIndex++`. The Lightbox can be opened from anywhere (highlights, recent activity, etc.) and shares its verdict logic.

These already call `_updateGridCard` (now an alias) at 2 spots. We want them to ALSO call `applyVerdictToUI` directly so:
- Naming consistency
- The grid behind the lightbox (e.g., when lightbox opened from Classified) gets the new animate-out
- They participate in `recordVerdictOnQueue` if a queue state is in play

- [ ] **Step 9.1: Audit verdict call sites**

```bash
cd /Users/vives/bird-classifier
grep -n -B2 -A8 'submitReview\|lightboxReview\|submitWrongCorrection\|submitLightboxWrong\|useAsCorrection\b' dashboard/index.html | grep -E '\bfunction\b|reviewSubmit2|_updateGridCard|applyVerdictToUI|reviewIndex' | head -50
```

- [ ] **Step 9.2: Replace `_updateGridCard(...)` calls with `applyVerdictToUI(...)`**

In each verdict path, change:
```javascript
_updateGridCard(file, verdict, correctSpecies);
```
To:
```javascript
applyVerdictToUI(file, verdict, correctSpecies);
```

The behavior is identical (alias) but new callers should use the new name.

- [ ] **Step 9.3: Add queue-state recording where applicable**

For `submitReview` (the Classify sub-tab path), there's no per-tab queue state today (it tracks `reviewIndex` against `pendingReview` array). Don't add queue state here unless it makes sense — auto-advance via `reviewIndex++` is the existing pattern. Leave it.

For `lightboxReview` when the lightbox was opened from a grid sub-tab: pass through to `applyVerdictToUI` (already done in 9.2). The grid behind the lightbox will animate the card out.

- [ ] **Step 9.4: Smoke-test all opening contexts of the lightbox**

In browser:
1. Open lightbox from Recent Activity → click trash → close lightbox → expected: card in Recent strip is gone (or shows Confirmed badge for correct)
2. Open lightbox from Classified grid → click trash → close → expected: classified card animated out
3. Open lightbox from species-profile gallery → click trash → close → expected: gallery thumbnail animated out (if applicable)

- [ ] **Step 9.5: Commit**

```bash
cd /Users/vives/bird-classifier
git add dashboard/index.html
git commit -m "feat(review): Classify+Lightbox verdict paths use applyVerdictToUI by name

All verdict paths in the Review tab now call applyVerdictToUI directly
instead of the _updateGridCard alias. Behavior unchanged; the alias stays
for backward-compatibility with any external code that reaches in by name.
This completes the Review-tab migration to the shared verdict-UI helper."
```

---

## Task 10: Add camera + multibird filters to `/api/review/classified`

**Files:**
- Modify: `dashboard/api.py:2499-2528` (`review_classified` endpoint)

The Classified endpoint today filters only by `species` and `verdict`. The
Pending endpoint already supports `camera` and `multibird`. Make Classified
match so the UI can offer the same filtering on both tabs.

- [ ] **Step 10.1: Read the existing pending endpoint to copy the filter pattern**

```bash
cd /Users/vives/bird-classifier
grep -n -A30 'def review_pending' dashboard/api.py | head -40
```

Note how `multibird` and `camera` are passed into `rdb.get_classifications()` and
`rdb.count_classifications()`.

- [ ] **Step 10.2: Add the same params to `review_classified`**

Find `def review_classified(species: str = "", verdict: str = "", limit: int = 50, offset: int = 0):`
at line ~2499. Replace the signature with:

```python
def review_classified(species: str = "", verdict: str = "", camera: str = "",
                      multibird: str = "", limit: int = 50, offset: int = 0):
    """Get reviewed classifications (correct, wrong, reclassify verdicts).

    Filters:
      species   — exact common_name match
      verdict   — 'correct' | 'wrong' | (empty = all)
      camera    — 'feeder' | 'ground' | (empty = all)
      multibird — 'only' (just multi-bird frames), 'exclude' (single-bird only),
                  '' (all)

    Uses SQL JOIN via reviews_db instead of batch file lookup.
    """
```

Then in the body, replace the two `rdb.get_classifications(...)` and
`rdb.count_classifications(...)` calls to pass `camera` and `multibird`:

```python
    sp = species or None
    v = verdict or None
    cam = camera or None
    mb = multibird or None
    rows = rdb.get_classifications(status="reviewed", species=sp, verdict=v,
                                    camera=cam, multibird=mb,
                                    offset=offset, limit=limit)
    total = rdb.count_classifications(status="reviewed", species=sp, verdict=v,
                                       camera=cam, multibird=mb)
```

- [ ] **Step 10.3: Verify `rdb.get_classifications` accepts `camera` and `multibird` kwargs**

```bash
cd /Users/vives/bird-classifier
grep -n -A10 'def get_classifications' reviews_db.py | head -25
```

If `camera` and `multibird` are NOT already accepted there, this task balloons:
the helper needs the params added too. Pause and report — do not silently
extend `reviews_db.py` without surfacing it.

If they ARE already accepted (because pending uses them), you're done with
the server side.

- [ ] **Step 10.4: Restart dashboard + smoke-test the new server params**

```bash
launchctl kickstart -k "gui/$(id -u)/com.vives.bird-dashboard" && sleep 4
curl -sS 'http://localhost:8099/api/review/classified?camera=feeder&multibird=exclude&limit=3' \
  | python3 -m json.tool | head -30
```

Expected: returns 3 single-bird feeder-camera reviewed items. Verify by checking
that each `item.file` starts with `feeder_` (per the camera-prefix filename
convention).

- [ ] **Step 10.5: Commit**

```bash
cd /Users/vives/bird-classifier
git add dashboard/api.py
git commit -m "feat(review): /api/review/classified accepts camera + multibird filters

Brings the Classified endpoint to parity with /api/review/pending so the UI
can offer camera and multi-bird filtering on both Classify and Classified
sub-tabs. Multi-bird filtering matters for training-data curation: single-
bird frames are unambiguous; multi-bird frames have ambiguous which-label-
applies-to-which-bird semantics."
```

---

## Task 11: Add camera + multibird filter dropdowns to Classify and Classified UIs

**Files:**
- Modify: `dashboard/index.html` Classify sub-tab (find via `id="subtab-classify"`)
- Modify: `dashboard/index.html` Classified sub-tab (lines ~2562-2580 and ~5847-5942 from Task 5)

- [ ] **Step 11.1: Find the Classify sub-tab toolbar**

```bash
cd /Users/vives/bird-classifier
grep -n -B1 -A30 'id="subtab-classify"' dashboard/index.html | head -40
```

Locate the toolbar where the species filter (or any filter) currently sits. If
no filter toolbar exists, place the dropdowns in the natural top-of-content
location for the sub-tab.

- [ ] **Step 11.2: Add dropdowns to Classify sub-tab**

In the Classify sub-tab toolbar, add two new `<select>` elements alongside any
existing filters. Use the same styling as the existing dropdowns (copy
class+style from the Classified tab's species-filter at line ~2565).

```html
<select id="classify-camera-filter" onchange="loadReviewItems()" style="padding:6px 10px;border-radius:6px;border:1px solid var(--border);background:var(--bg-card);color:var(--text-primary);font-size:0.85rem;">
  <option value="">All Cameras</option>
  <option value="feeder">Feeder</option>
  <option value="ground">Ground</option>
</select>
<select id="classify-multibird-filter" onchange="loadReviewItems()" style="padding:6px 10px;border-radius:6px;border:1px solid var(--border);background:var(--bg-card);color:var(--text-primary);font-size:0.85rem;">
  <option value="">All Shots</option>
  <option value="exclude">Single-bird only</option>
  <option value="only">Multi-bird only</option>
</select>
```

(`loadReviewItems()` is the existing Classify loader. If it's named differently,
substitute the actual function name.)

- [ ] **Step 11.3: Pass the new filter values from Classify loader to the API**

Find `loadReviewItems` (or whatever fetches `/api/review/pending` for Classify).
Currently it likely builds a URL like:

```javascript
var url = '/bird-api/review/pending?offset=' + _reviewOffset + '&limit=' + _REVIEW_PAGE_SIZE;
if (speciesFilter) url += '&species=' + encodeURIComponent(speciesFilter);
```

Add:

```javascript
var cameraFilter = document.getElementById('classify-camera-filter').value;
if (cameraFilter) url += '&camera=' + encodeURIComponent(cameraFilter);
var multibirdFilter = document.getElementById('classify-multibird-filter').value;
if (multibirdFilter) url += '&multibird=' + encodeURIComponent(multibirdFilter);
```

- [ ] **Step 11.4: Add dropdowns to Classified sub-tab**

In `dashboard/index.html` around line 2565 (the existing species + verdict filters
in the Classified toolbar), add the same two `<select>`s with IDs
`classified-camera-filter` and `classified-multibird-filter`. Wire `onchange`
to `loadClassifiedItems()`.

- [ ] **Step 11.5: Pass the new filter values from Classified loader to the API**

In the `loadClassifiedItems` (now using `loadQueue` per Task 5), update the
filter-population block (Step 5.2 had `classifiedQueueState.params.species` and
`.verdict`). Add:

```javascript
classifiedQueueState.params.camera =
  document.getElementById('classified-camera-filter').value || '';
classifiedQueueState.params.multibird =
  document.getElementById('classified-multibird-filter').value || '';
```

`loadQueue` will pass them through as query params automatically.

- [ ] **Step 11.6: Hard-refresh + manual end-to-end on both tabs**

```bash
launchctl kickstart -k "gui/$(id -u)/com.vives.bird-dashboard" && sleep 4
```

In browser:
1. Open Review tab → Classify sub-tab
2. Set Camera = "Feeder", Multi-bird = "Single-bird only"
3. Expected: queue shows only feeder-camera single-bird items
4. Repeat in Classified sub-tab — same expected behavior

- [ ] **Step 11.7: Commit**

```bash
cd /Users/vives/bird-classifier
git add dashboard/index.html
git commit -m "feat(review): camera + multibird filters on Classify and Classified tabs

Both tabs now expose dropdowns for camera (feeder/ground/all) and multi-bird
shot filtering (single-only/multi-only/all). Single-bird filtering is the
training-data-curation default since multi-bird frames have ambiguous
label-to-bird mapping."
```

---

## Task 12: Document the helpers

**Files:**
- Create: `docs/superpowers/specs/2026-04-25-review-ui-helpers.md`

- [ ] **Step 10.1: Write the spec doc**

Create the file with this content:

```markdown
# Review UI Shared Helpers

**Audience:** future engineers / Claude touching the Review tab in dashboard/index.html.

## What

Two JS helpers in `dashboard/index.html` that all Review-tab sub-tabs use for
verdict-handling and pagination:

- `applyVerdictToUI(file, verdict, correctSpecies) → bool` — single source of truth for
  what a verdict looks like in the UI. Returns `true` if a card was removed
  from the DOM (caller may want to refetch when grid empties).
  - `trash` / `wrong` → animate out + remove from DOM
  - `correct` → green Confirmed badge, card stays
  - `skip` → gray Skipped badge, card stays
  - `reclassify` → amber Re-queued badge, card stays
- `loadQueue(state, renderFn)` + `queueNextPage(state, renderFn)` +
  `queuePrevPage(state, renderFn)` + `recordVerdictOnQueue(state, removed)` —
  shared pagination that tracks verdicts-since-last-fetch and adjusts offset
  on Next-page so items pulled up from the next page are not skipped.

## Why

Before this refactor, each sub-tab (Classify, Classified, Skipped, Missed, Batch)
had its own offset variable + its own verdict-UI behavior. Trash on the
Classified tab grayed out the card; trash on the Lightbox auto-advanced;
trash on Batch removed from selection. Inconsistent. Pagination after several
verdicts could skip rows because offset didn't account for the shrunken result
set.

## How to add a new paginated review surface

```javascript
var myQueueState = {
  endpoint: '/bird-api/my-queue',
  pageSize: 24,
  offset: 0,
  verdictsSinceFetch: 0,
  params: { species: '', verdict: '' },  // any extra filter params
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

## Files

- Helper definitions: `dashboard/index.html` (search for `applyVerdictToUI` and `loadQueue`)
- All current callers: Classified (`loadClassifiedItems`), Skipped (`loadSkippedFrames`),
  Missed (`loadMissedBirds`), Batch (`loadBatchReview` + batch confirm/reject),
  Classify (one-at-a-time via `submitReview`), Lightbox (`lightboxReview`)

## Migration history

Implemented 2026-04-25 per `docs/superpowers/plans/2026-04-25-review-ui-shared-helpers.md`.
Replaces the previous `_updateGridCard()` (kept as a thin alias).
```

- [ ] **Step 10.2: Commit**

```bash
cd /Users/vives/bird-classifier
git add docs/superpowers/specs/2026-04-25-review-ui-helpers.md
git commit -m "docs(review): spec for applyVerdictToUI + loadQueue shared helpers"
```

---

## Self-Review (writing-plans skill checklist)

**1. Spec coverage:**
- ✅ "Trash → gone immediately" → Task 1 changes the helper to remove from DOM
- ✅ "Wrong → goes to corrected species" → server already moves the file; UI just removes the card per Task 1
- ✅ "Same code as a helper across all sub-tabs" → Tasks 5-9 migrate every paginated/verdict sub-tab
- ✅ "No rows lost in pagination" → Task 4 introduces `verdictsSinceFetch` adjustment

**2. Placeholder scan:**
- ⚠️ Task 5.3 says "copy the existing grid-building HTML logic from old loadClassifiedItems here." This is a placeholder unless the engineer reads the existing code. Acceptable because the existing code IS the spec for the visual output (don't change visuals, just relocate). Engineer can grep for the old code easily.
- ⚠️ Task 6.3 same note. Same justification.
- ⚠️ Task 7.1 has a fork in the road ("if pagination exists / if not"). This is acceptable because we don't yet know — engineer audits and picks the right path with the patterns provided.

**3. Type consistency:**
- ✅ State object shape: `{endpoint, pageSize, offset, verdictsSinceFetch, params, lastResp}` is the same across all uses (Tasks 4, 5, 6).
- ✅ `applyVerdictToUI(file, verdict, correctSpecies) → bool` signature consistent across Tasks 1, 5, 6, 7, 8, 9.
- ✅ `recordVerdictOnQueue(state, removed)` consistent.

**4. Out of scope (explicitly NOT in this plan):**
- The Manage sub-tab (it's an inventory view, no verdict flow)
- Server-side endpoints (we use existing `/api/review/{classified,pending,smart-queue,batch,skipped,missed}` and the new `/api/review2/queue`)
- The Lightbox itself's nav buttons (← →) — those aren't verdict actions
- Playwright integration tests — manual smoke-tests per task are the verification mechanism for this UI refactor; Playwright tests can be added as a follow-up if desired