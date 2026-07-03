#!/usr/bin/env python3
"""MSE-only rig leg: confirm the -67ms per-transport trim centers O_total."""
import importlib.util, json, sys, time

spec = importlib.util.spec_from_file_location(
    "sync_rig", "/tmp/sync-rig/sync_rig.py")
rig = importlib.util.module_from_spec(spec)
sys.modules["sync_rig"] = rig
# prevent main() from running on import: sync_rig guards with __main__ ✓
spec.loader.exec_module(rig)

from playwright.sync_api import sync_playwright

gt = rig.load_gt(f"{rig.SCRATCH}/gt_events_tc.jsonl")
rig.demo_mode(True); time.sleep(12)
try:
    with sync_playwright() as p:
        b = p.chromium.launch()
        pg = b.new_page(viewport={"width": 1100, "height": 700})
        pg.goto("http://pi5.local:8099/", wait_until="domcontentloaded", timeout=30000)
        pg.wait_for_function("() => { const h=document.querySelector('video-stream'); const v=h&&h.querySelector('video'); return v && v.readyState >= 2 && !v.paused; }", timeout=30000)
        time.sleep(2)
        pg.evaluate(rig.SAMPLER_SETUP)
        pg.evaluate("() => { const h=document.querySelector('video-stream'); h.mode='mse'; if (h.ondisconnect) h.ondisconnect(); h.src='ws://'+location.host+'/api/ws?src=feeder-demo'; }")
        print("MSE forced; calibrating…"); time.sleep(16)
        rep = rig.analyze(rig.collect(pg, 60), gt, "mse-trimmed")
        print(json.dumps(rep))
        b.close()
finally:
    rig.demo_mode(False)
    print("demo OFF, live restored")
sys.exit(0 if rep.get("PASS") else 1)
