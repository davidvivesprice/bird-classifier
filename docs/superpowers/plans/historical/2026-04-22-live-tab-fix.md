> **HISTORICAL** — This document describes a design, plan, or system state that has been superseded. Kept for decision-trail context.

# Live Tab Fix Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix four Live-tab regressions David reported on 2026-04-22:
- **0a-1**: default feeder stream doesn't auto-load on fresh page (LAN + tunnel)
- **0a-2**: after ground→feeder toggle, video renders tiny (LAN + tunnel)
- **0a-3**: through tunnel, "Connecting to camera" overlay stays visible even when video is playing
- **0a-4**: fullscreen button doesn't work on phone via LAN

**Architecture:** Recon revealed a single causal chain for three of the four bugs. The 8-second "offline timeout" in `initLiveFeed()` (index.html:3438–3452) mutates the DOM destructively: sets `wrapper.style.paddingBottom='30%'`, changes the offline text to "Camera offline," and hides four control buttons. **None of this damage is reset** on the next `initLiveFeed()` call (switchStream / page re-init). So on fresh page load the initial connect races this 8s timeout; if it loses once, the wrapper stays squished (→ tiny video, 0a-2) and the fs button stays hidden (→ no fullscreen, 0a-4) forever. The overlay-not-hiding bug (0a-3) is a separate issue: the polling loop at line 3468 watches `live-video` readyState, which on MSE-over-tunnel doesn't reach 2 reliably; switch to the video element's native `playing` event.

Fix = three surgical patches to `dashboard/index.html`:
1. Reset timeout-inflicted damage at the top of every `initLiveFeed()` entry.
2. Replace readyState polling with `playing` event listener.
3. Soften the 8s "offline" timeout so it doesn't kill controls prematurely on slow tunnel starts.

**Tech Stack:** HTML5 custom elements, VideoRTC (go2rtc), no backend changes.

---

## File Structure

**Files modified:**
- `~/bird-classifier/dashboard/index.html` — `initLiveFeed()` at lines 3408–3475 and `toggleLiveFullscreen` if verification turns up CSS issues.

**No new files, no backend changes.**

---

### Task 1: Reset offline-timeout DOM damage on every initLiveFeed entry

**Files:**
- Modify: `dashboard/index.html:3409-3436` — `initLiveFeed()` top.

- [ ] **Step 1.1: Read the current function to confirm line numbers**

  Run:
  ```bash
  grep -n "function initLiveFeed" ~/bird-classifier/dashboard/index.html
  ```
  Expected: `3409:  function initLiveFeed() {`

- [ ] **Step 1.2: Insert reset block at the top of `initLiveFeed()`**

  After the `if (!wrapper) return;` (line ~3412) and before the `customElements.get('video-rtc')` check (line ~3415), insert:

  ```js
      // Reset any "offline timeout" damage from a prior init. Without this,
      // one failed initial connect leaves wrapper squished and fs button
      // hidden forever (0a-2, 0a-4).
      wrapper.style.paddingBottom = '';
      ['stream-switcher','overlay-toggle','live-mute-btn','live-fs-btn'].forEach(function(id) {
        var el = document.getElementById(id);
        if (el) el.style.display = '';
      });
      var offlineText = document.getElementById('live-offline-text');
      if (offlineText) offlineText.textContent = 'Connecting to camera...';
      var offlineHint = document.getElementById('live-offline-hint');
      if (offlineHint) offlineHint.style.display = 'none';
      if (offline) offline.style.display = '';  // show overlay until first frame
  ```

  This reverses every DOM mutation the 8s offline-timeout makes, every time the function runs.

- [ ] **Step 1.3: Verify by reload + switch test (manual)**

  After the edit:
  1. Restart the dashboard only if it serves from memory: `launchctl kickstart -k gui/$(id -u)/com.vives.bird-dashboard` (uvicorn serves the file from disk per request, so no restart is usually needed; reload in browser with Cmd+Shift+R).
  2. Load the dashboard on LAN (`http://localhost:8099`).
  3. Wait 10s on fresh page. If it ever shows "Camera offline" + tiny video, that's the pre-fix behavior.
  4. Click Ground → Feeder. Video should be full-size (not tiny), controls visible.

  Evidence gate: David reports video renders full-size after switch, fs button visible. **Do not mark this task done until David confirms.**

---

### Task 2: Replace readyState polling with `playing` event

**Files:**
- Modify: `dashboard/index.html:3467-3474` — the `setInterval(checkPlaying, 500)` block.

- [ ] **Step 2.1: Replace poll with event listener**

  Current:
  ```js
      // Hide offline indicator once video starts playing
      var checkPlaying = setInterval(function() {
        var vid = document.getElementById('live-video');
        if (vid && vid.readyState >= 2) {
          if (offline) offline.style.display = 'none';
          clearInterval(checkPlaying);
        }
      }, 500);
  ```

  Replace with:
  ```js
      // Hide offline indicator as soon as the video element exists AND fires
      // `playing`. readyState polling was unreliable on MSE-over-tunnel
      // (0a-3) — the `playing` event fires on all transport paths (WebRTC,
      // MSE, HLS, MP4) as soon as frames actually decode.
      var hideOffline = function() {
        if (offline) offline.style.display = 'none';
      };
      var attachPlayingListener = function() {
        var vid = document.getElementById('live-video');
        if (!vid) return false;
        vid.addEventListener('playing', hideOffline, { once: true });
        // If already playing (rare race), hide immediately.
        if (!vid.paused && vid.readyState >= 3) hideOffline();
        return true;
      };
      // `live-video` is only given that id inside BirdVideoRTC.oninit, which
      // runs after connectedCallback. Retry briefly until it exists.
      if (!attachPlayingListener()) {
        var retries = 0;
        var retry = setInterval(function() {
          if (attachPlayingListener() || ++retries > 20) clearInterval(retry);
        }, 200);
      }
  ```

- [ ] **Step 2.2: Verify over tunnel**

  David reloads `https://birds.vivessato.com` and reports: does the "Connecting to camera" overlay go away when video appears?

  Evidence gate: David confirms overlay hides within 1–2 seconds of first frame, on tunnel AND LAN.

---

### Task 3: Soften the 8s offline timeout

**Files:**
- Modify: `dashboard/index.html:3438-3452` — the `setTimeout(..., 8000)` block.

- [ ] **Step 3.1: Change the timeout to show hint only, keep controls visible**

  Current block hides controls AND sets `paddingBottom='30%'` AND changes text. The hint is useful ("Camera feeds require local network access" — helps remote users understand the tunnel path). The button-hiding and wrapper-squishing are destructive.

  Replace:
  ```js
      // Show helpful hint and hide controls if camera doesn't connect within 8s
      setTimeout(function() {
        var hint = document.getElementById('live-offline-hint');
        var text = document.getElementById('live-offline-text');
        var vid = document.getElementById('live-video');
        if (hint && offline && (!vid || vid.readyState < 2)) {
          text.textContent = 'Camera offline';
          hint.style.display = '';
          var wrapper = document.getElementById('live-wrapper');
          if (wrapper) wrapper.style.paddingBottom = '30%';
          ['stream-switcher','overlay-toggle','live-mute-btn','live-fs-btn'].forEach(function(id) {
            var el = document.getElementById(id);
            if (el) el.style.display = 'none';
          });
        }
      }, 8000);
  ```

  with:
  ```js
      // After 8s with no video, surface a hint. Don't destroy the wrapper's
      // layout or hide the controls — a slow tunnel start still deserves an
      // un-mutilated UI when the frame finally arrives.
      setTimeout(function() {
        var vid = document.getElementById('live-video');
        if (vid && !vid.paused && vid.readyState >= 2) return;
        var hint = document.getElementById('live-offline-hint');
        if (hint) hint.style.display = '';
      }, 8000);
  ```

- [ ] **Step 3.2: Verify cold-start UX on fresh tab**

  David closes all browser tabs on `birds.vivessato.com`, opens a fresh one, waits. Expected: within ~8s the hint appears under "Connecting to camera..." but controls and wrapper sizing are intact. When the video finally arrives, overlay hides, controls still visible, video fills the wrapper.

  Evidence gate: David confirms the fresh-page load now shows video (0a-1) within ~15s on both LAN and tunnel, no tiny video, no missing fs button.

---

### Task 4: Final regression check

- [ ] **Step 4.1: Run full verification pass**

  Ask David to exercise the Live tab in this order:
  1. Fresh browser tab → LAN URL → wait for feeder to appear. ✅ default loads.
  2. Click Ground → wait for ground to appear. ✅ switches.
  3. Click Feeder → wait for feeder to appear. ✅ not tiny.
  4. Click Both → split view appears.
  5. Click fullscreen button on phone via LAN. ✅ expanded mode activates.
  6. Repeat #1–5 on `https://birds.vivessato.com`. ✅ same results; no stuck overlay.

- [ ] **Step 4.2: Commit**

  ```bash
  cd ~/bird-classifier
  git add dashboard/index.html
  git commit -m "$(cat <<'EOF'
  Fix Live tab regressions: default load, post-switch sizing, stuck overlay, fullscreen

  Three small patches to initLiveFeed() in dashboard/index.html:
  1. Reset DOM damage from the "offline timeout" on every init entry —
     fixes tiny-video-after-switch (0a-2) and lost fullscreen button (0a-4).
  2. Replace readyState polling with the video's `playing` event — the old
     poll never triggered reliably on MSE-over-tunnel, leaving the
     "Connecting to camera" overlay stuck over live video (0a-3).
  3. Soften the 8s offline timeout: show a hint only; don't hide controls
     or squish the wrapper. Slow tunnel starts now render correctly when
     the first frame arrives (0a-1).

  No backend changes.

  Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
  EOF
  )"
  ```

---

## Self-review notes

- **Spec coverage:** 0a-1 (Task 3), 0a-2 (Task 1), 0a-3 (Task 2), 0a-4 (Task 1) — all four named bugs have a specific task.
- **Placeholder scan:** none.
- **Type consistency:** element IDs (`live-video`, `live-wrapper`, `live-offline`, `live-offline-text`, `live-offline-hint`, `stream-switcher`, `overlay-toggle`, `live-mute-btn`, `live-fs-btn`) all match the current codebase (verified in recon).
- **Risk:** the root cause is hypothesized from code reading, not reproduced in a test. If Task 1 doesn't resolve 0a-2/0a-4, the symptoms expose a different root cause and we iterate — the patches are harmless even if the hypothesis is partly wrong. Evidence gates after each task keep us honest.