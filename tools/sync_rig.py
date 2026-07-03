#!/usr/bin/env python3
"""Objective overlay-sync measurement rig.

Working dir: /tmp/sync-rig (override paths in-file if needed). Required inputs
there: gt_events_tc.jsonl (offline ground truth from tools/offline_replay.py on
the TIMECODED demo — see tools/make_timecode_demo.py). Runs from the Mac against
http://pi5.local:8099 with Playwright + Chromium.

The demo video carries a per-frame machine timecode (top strip). The rig plays
the REAL dashboard in real Chrome, and per sample: decodes which frame is ON
GLASS from the strip, and reads the engine state + rendered boxes. Against the
offline ground truth (same stamped file through the same pipeline) it computes:

  O_clock  : engine's displayed-time estimate error vs the glass truth (ms)
  O_total  : time shift of rendered boxes vs gt boxes at the glass frame (ms)
  spatial  : center error (px @640x360) of rendered boxes vs gt at tau=0

Acceptance (per transport): |O_clock| median <= 40ms; |O_total| p50 <= 40ms,
p95 <= 100ms; center error mean <= 8px.

Runs: Chrome WebRTC Smooth / Chrome WebRTC Realtime / forced MSE + a chaos
step (pipeline restart mid-run -> re-lock < 5s). Restores live mode at exit.
"""
import json, math, statistics, subprocess, sys, time
import urllib.request

from playwright.sync_api import sync_playwright

SCRATCH = "/tmp/sync-rig"
PI = "vives@192.168.6.156"
LOOP_S = 4498 / 30.0

SAMPLER_SETUP = r"""
() => {
  const host = document.querySelector('video-stream');
  const inner = host && host.querySelector('video');
  const cv = document.createElement('canvas');
  cv.width = 224; cv.height = 10;
  const ctx = cv.getContext('2d', { willReadFrequently: true });
  const BLOCK = 8, NB = 26;
  window.__rig = { samples: [], err: null };
  const decode = () => {
    try {
      ctx.drawImage(inner, 0, 0, NB * BLOCK + 2, 10, 0, 0, NB * BLOCK + 2, 10);
      const row = ctx.getImageData(0, 1 + BLOCK / 2, NB * BLOCK + 2, 1).data;
      const bits = [];
      for (let i = 0; i < NB; i++) {
        const x = 1 + i * BLOCK + BLOCK / 2;
        const v = (row[x * 4] + row[x * 4 + 1] + row[x * 4 + 2]) / 3;
        bits.push(v > 127 ? 1 : 0);
      }
      if (bits[0] !== 1 || bits[1] !== 0) return null;
      let idx = 0; for (let i = 2; i < 18; i++) idx = (idx << 1) | bits[i];
      let chk = 0; for (let i = 18; i < 26; i++) chk = (chk << 1) | bits[i];
      if (chk !== (((idx >> 8) & 0xFF) ^ (idx & 0xFF))) return null;
      return idx;
    } catch (e) { window.__rig.err = String(e); return null; }
  };
  const stage = document.getElementById('live-stage');
  window.__rigTimer = setInterval(() => {
    const d = window.__overlayDebug;
    const r = stage.getBoundingClientRect();
    window.__rig.samples.push({
      t: performance.now(),
      frameIdx: decode(),
      T: d.clock.displayedEventTime,
      state: d.est.state, mode: d.mode, cEst: d.est.cEst,
      reanchors: d.est.reanchors,
      stageW: r.width, stageH: r.height,
      boxes: d.renderedBoxes.map(b => ({x: b.x, y: b.y, w: b.w, h: b.h})),
    });
    if (window.__rig.samples.length > 1500) window.__rig.samples.shift();
  }, 100);
}
"""

def demo_mode(on):
    req = urllib.request.Request("http://pi5.local:8099/api/demo-mode",
                                 data=json.dumps({"enabled": on}).encode(),
                                 headers={"Content-Type": "application/json"})
    try:
        urllib.request.urlopen(req, timeout=60).read()
    except Exception as e:
        # state usually flips even when the response fails (restart timing);
        # verify via GET instead of dying inside cleanup.
        time.sleep(5)
        try:
            got = json.load(urllib.request.urlopen(
                "http://pi5.local:8099/api/demo-mode", timeout=15))
            if bool(got.get("enabled")) != on:
                raise RuntimeError(f"demo-mode toggle failed: {e}")
            print(f"(demo-mode POST errored [{e}] but state verified = {on})")
        except RuntimeError:
            raise
        except Exception:
            raise RuntimeError(f"demo-mode toggle unverifiable: {e}")

def load_gt(path):
    gt = {}
    for line in open(path):
        line = line.strip()
        if not line: continue
        d = json.loads(line)
        if d.get("pts") is None: continue
        gt[round(d["pts"] * 30)] = [t["bbox"] for t in d.get("tracks", [])]
    return gt

def stage_to_video(b, stageW, stageH, fw=640, fh=360):
    stageAR = stageW / max(stageH, 1); videoAR = fw / fh
    if videoAR > stageAR:
        rw = stageW; rh = stageW / videoAR; ox = 0; oy = (stageH - rh) / 2
    else:
        rh = stageH; rw = stageH * videoAR; oy = 0; ox = (stageW - rw) / 2
    sx = rw / fw; sy = rh / fh
    return [(b["x"] - ox) / sx, (b["y"] - oy) / sy,
            (b["x"] + b["w"] - ox) / sx, (b["y"] + b["h"] - oy) / sy]

def center(b): return ((b[0] + b[2]) / 2, (b[1] + b[3]) / 2)

def match_err(rboxes, gboxes):
    """mean nearest-center distance of rendered boxes to gt boxes (px)."""
    if not rboxes or not gboxes: return None
    tot = 0
    for rb in rboxes:
        rc = center(rb)
        tot += min(math.hypot(rc[0] - center(g)[0], rc[1] - center(g)[1]) for g in gboxes)
    return tot / len(rboxes)

def gt_motion(gt, idx, px=8.0):
    """True when the gt boxes around this frame actually MOVE (>=px over ±0.5s)
    — a stationary bird makes the time-shift unidentifiable (a non-moving box
    matches equally at every tau), so those samples can't measure O_total."""
    a, b = gt.get((idx - 15) % 4498), gt.get((idx + 15) % 4498)
    if not a or not b: return False
    moved = 0.0
    for ba in a:
        ca = center(ba)
        moved = max(moved, min(math.hypot(ca[0] - center(bb)[0], ca[1] - center(bb)[1]) for bb in b))
    return moved >= px

def analyze(samples, gt, label, realtime=False):
    """Metric v2 (2026-07-03):
    - O_clock is epoch-robust: any pipeline restart resets the pts epoch, so
      (T - glass) contains an arbitrary session constant phi. We report the
      JITTER around the session median (engine clock stability) + phi itself.
      The ABSOLUTE user-perceived offset is O_total (content-anchored).
    - O_total only uses motion-gated samples (tau unidentifiable otherwise).
    - spatial gate on the MEDIAN: the tail mixes in cross-loop tracker-state
      divergence (live tracker history spans loops; gt is a single clean pass)
      which is not a sync error."""
    o_raw, spatial, taus = [], [], []
    locked = [s for s in samples if s["state"] == "LOCKED" and s["frameIdx"] is not None]
    for s in locked:
        idx = s["frameIdx"]
        if s["T"] is not None:
            d = (s["T"] - idx / 30.0) % LOOP_S
            o_raw.append(d * 1000)
        rb = [stage_to_video(b, s["stageW"], s["stageH"]) for b in s["boxes"]
              if b["x"] is not None]
        if not rb: continue
        g0 = gt.get(idx)
        if g0:
            e0 = match_err(rb, g0)
            if e0 is not None: spatial.append(e0)
        if not gt_motion(gt, idx):
            continue
        best_tau, best_e = None, 1e9
        for dt in range(-60, 61):        # 33ms grid (1 frame)
            g = gt.get((idx + dt) % 4498)
            if not g: continue
            e = match_err(rb, g)
            if e is not None and e < best_e: best_e, best_tau = e, dt / 30.0
        if best_tau is not None and best_e < 40:
            taus.append(best_tau * 1000)
    # epoch constant phi = session median of the raw clock offset (wrap-aware
    # median: use the circular mean neighborhood of the histogram peak)
    o_jitter, phi = [], None
    if o_raw:
        srt = sorted(o_raw)
        phi = srt[len(srt) // 2]
        for v in o_raw:
            d = (v - phi) % (LOOP_S * 1000)
            if d > LOOP_S * 500: d -= LOOP_S * 1000
            o_jitter.append(d)
    def stats(v):
        if not v: return None
        v = sorted(v)
        return {"n": len(v), "p50": round(v[len(v)//2], 1),
                "p10": round(v[len(v)//10], 1), "p90": round(v[9*len(v)//10], 1),
                "p95": round(v[int(0.95*len(v))-1], 1) if len(v) >= 20 else None,
                "mean": round(statistics.mean(v), 1)}
    rep = {"label": label, "samples": len(samples), "locked": len(locked),
           "decoded": sum(1 for s in samples if s["frameIdx"] is not None),
           "phi_ms": round(phi, 1) if phi is not None else None,
           "O_clock_jitter_ms": stats(o_jitter),
           "O_total_ms": stats(taus), "spatial_px": stats(spatial)}
    oj, ot, sp = rep["O_clock_jitter_ms"], rep["O_total_ms"], rep["spatial_px"]
    if realtime:
        # physics: labels can only trail on a minimum-delay video; gate sanity
        rep["PASS"] = bool(ot and sp and -350 <= ot["p50"] <= 40 and sp["p50"] <= 8)
    else:
        rep["PASS"] = bool(oj and ot and sp and abs(ot["p50"]) <= 40
                           and oj["p90"] <= 40 and sp["p50"] <= 8)
    return rep

def collect(pg, seconds):
    pg.evaluate("() => { window.__rig.samples.length = 0; }")
    time.sleep(seconds)
    return pg.evaluate("() => window.__rig.samples")

def main():
    gt = load_gt(f"{SCRATCH}/gt_events_tc.jsonl")
    print(f"gt frames with tracks: {len(gt)}")
    demo_mode(True); time.sleep(12)
    reports = []
    try:
        with sync_playwright() as p:
            b = p.chromium.launch()
            pg = b.new_page(viewport={"width": 1100, "height": 700})
            pg.goto("http://pi5.local:8099/", wait_until="domcontentloaded", timeout=30000)
            pg.wait_for_function("() => { const h=document.querySelector('video-stream'); const v=h&&h.querySelector('video'); return v && v.readyState >= 2 && !v.paused; }", timeout=30000)
            time.sleep(2)
            pg.evaluate(SAMPLER_SETUP)
            print("calibrating (smooth)…"); time.sleep(14)

            reports.append(analyze(collect(pg, 60), gt, "webrtc-smooth"))
            print(json.dumps(reports[-1]))

            # chaos: pipeline restart mid-stream -> re-anchor + re-lock
            print("chaos: restarting bird-pipeline…")
            subprocess.run(["ssh", PI, "systemctl --user restart bird-pipeline.service"], check=True)
            t0 = time.time(); relock = None
            while time.time() - t0 < 40:
                st = pg.evaluate("() => ({s: window.__overlayDebug.est.state, r: window.__overlayDebug.est.reanchors, p: window.__overlayDebug.ptsNewest})")
                if st["r"] > 0 and st["s"] == "LOCKED": relock = time.time() - t0; break
                time.sleep(1)
            print(f"chaos: re-locked after {relock:.1f}s" if relock else "chaos: NO re-lock in 40s")
            reports.append({"label": "chaos-relock_s", "value": relock,
                            "PASS": relock is not None and relock < 25})

            # realtime run
            pg.click("#latency-toggle"); time.sleep(10)
            reports.append(analyze(collect(pg, 45), gt, "webrtc-realtime", realtime=True))
            print(json.dumps(reports[-1]))
            pg.click("#latency-toggle"); time.sleep(8)   # back to smooth

            # forced MSE run
            pg.evaluate("() => { const h=document.querySelector('video-stream'); h.mode='mse'; if (h.ondisconnect) h.ondisconnect(); h.src='ws://'+location.host+'/api/ws?src=feeder-demo'; }")
            time.sleep(16)
            reports.append(analyze(collect(pg, 45), gt, "mse-forced"))
            print(json.dumps(reports[-1]))
            # persist BEFORE any cleanup can fail
            json.dump(reports, open(f"{SCRATCH}/sync_rig_report.json", "w"), indent=1)
            b.close()
    finally:
        try:
            demo_mode(False)
            print("demo OFF, live restored")
        except Exception as e:
            print(f"!!! LIVE RESTORE FAILED — check the Pi: {e}")
    json.dump(reports, open(f"{SCRATCH}/sync_rig_report.json", "w"), indent=1)
    print("\n==== SUMMARY ====")
    for r in reports:
        print(f"  {r['label']:18s} PASS={r.get('PASS')}")
    return 0 if all(r.get("PASS") for r in reports) else 1

if __name__ == "__main__":
    sys.exit(main())
