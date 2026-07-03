# Documentation Audit

**Date:** 2026-07-03 (supersedes the 2026-04-26 audit)
**Repo:** /Users/vives/bird-classifier-pi (+ reference book at ~/docs/bird-observatory-pi/)
**Docs audited:** book chapters 00–10 + README + _truth-staging; repo CLAUDE.md, GUIDE.md, ROADMAP.md, docs/README.md. (`docs/working/**` and `docs/historical/**` exempt: dated records.)
**Code roots scanned:** pipeline/, dashboard/, tools/, tests/, deploy/, bird_pipeline_v3.py, go2rtc config (Pi + local)

## Summary

| Bucket | Count |
|---|---|
| ✅ Verified | 137 (book) + repo living docs hand-verified |
| ⚠️ Drift | 70 book + 14 repo = 84 (all auto-fixed) |
| ❌ Hallucination | 2 (all fixed) |
| 🐛 Smell | 11 raw → 9 unique (6 fixed same-day, 1 tasked, 1 in a spawned task, 1 noted) |
| ⏭ Skipped | 46 (aspirational / historical / out-of-repo) |

Also in this pass (beyond the audit): book chapter `11-the-demo-lab.md` written; Act III added to ch10; catch-up section + reading path added to 00/README; crash-war section added to ch03.

---

## ✅ Verified

137 book claims matched the code (file:line evidence captured per claim).

<details>
<summary>Show verified-claim counts per doc group</summary>

- 00-overview.md, README.md, _truth-staging-2026-06-11.md: 14 verified
- 01-hardware.md, 02-services.md: 19 verified
- 03-pipeline.md, bird_pipeline_v3.py, frame_capture.py, frame_capture_proc.py, process_thread.py, tracker.py, snapshot_writer.py, hailo_detector.py, pi_classifier.py, calibration.py, model_registry.py, sse_events.py, motion_gate.py, hls_segmenter.py, hires_ring.py, health.py, classifier.py, classifications_db.py, solar_utils.py, api.py: 20 verified
- 04-hailo-engine.md: 16 verified
- 05-dashboard.md, 06-pi-review.md: 20 verified
- 07-thermal.md, 08-deployment.md: 23 verified
- 09-the-unified-brain.md: 14 verified
- 10-overlay-sync.md: 11 verified

</details>

---

## ⚠️ Drift (auto-fixed)

### 1. Runs YOLO + AIY classification on every frame that passes a motion gate
- **Doc:** `00-overview.md:3`
- **Code reality:** Hailo detector runs full-frame; MOG2 motion gate is bypassed (regions never computed on the Hailo path) (pipeline/hailo_detector.py:43 uses_motion_regions=False; pipeline/process_thread.py:110-111 regions=None when )
- **Fix applied:** Reworded to "on every frame (full-frame — the motion gate is bypassed on the Hailo path)"

### 2. Pipeline chain: substream → motion gate → Hailo YOLO → tracker → ...
- **Doc:** `00-overview.md:30`
- **Code reality:** Motion gate bypassed; decode+detect now run in a supervised child process (pipeline/frame_capture_proc.py:371-374 (PrecomputedDetector shim, uses_motion_regions=False keeps MOG2 bypass))
- **Fix applied:** Table cell now reads "substream → Hailo YOLO (full-frame, decode+detect in a supervised child process; motion gate bypassed) → tracker → ..."

### 3. Chapter-03 TOC entries repeat "frame capture → motion gate → Hailo YOLO"
- **Doc:** `00-overview.md:51 and README.md:14`
- **Code reality:** Same motion-gate bypass as above (pipeline/hailo_detector.py:43; pipeline/process_thread.py:106-111)
- **Fix applied:** Both TOC lines now say "Hailo YOLO (motion gate bypassed)"

### 4. Repo-split context doc lives at docs/working/progress/2026-04-25-pi-repo-split.md
- **Doc:** `00-overview.md:59 and README.md:37`
- **Code reality:** File moved to docs/historical/2026-04-25-pi-repo-split.md (ls: present in docs/historical/, absent from docs/working/progress/)
- **Fix applied:** README path repointed to docs/historical/; overview parenthetical notes the move

### 5. Chapters table implies book is 00-09
- **Doc:** `README.md:5-20 (chapters table)`
- **Code reality:** 10-overlay-sync.md exists (and is listed in 00-overview's tree) (ls /Users/vives/docs/bird-observatory-pi/10-overlay-sync.md)
- **Fix applied:** Added a row for 10 · Overlay Sync

### 6. Book is "not in any git repo, just on disk"
- **Doc:** `README.md:24`
- **Code reality:** ~/docs is a git work tree (toplevel /Users/vives/docs) (git -C ~/docs/bird-observatory-pi rev-parse --show-toplevel → /Users/vives/docs)
- **Fix applied:** Now reads "versioned as part of the ~/docs git repo"

### 7. 2026-04-25-pi5-handoff.md is the most recent end-of-session handoff
- **Doc:** `README.md:36`
- **Code reality:** Newer handoffs/progress docs exist (2026-05-11-handoff-to-conversational-claude.md, 2026-07-03-native-crash-isolation.md) (ls docs/working/progress/ shows May-July 2026 entries)
- **Fix applied:** Reworded: bring-up handoff; newer handoffs sit alongside in progress/

### 8. Progress bucket count = 13 (plus artifact dir)
- **Doc:** `README.md:48`
- **Code reality:** 12 progress docs + the 2026-04-11-v3-verification/ dir (13 dir entries total) (ls docs/historical/progress/: 12 .md files + 1 dir)
- **Fix applied:** Count changed 13 → 12

### 9. feeder-sub used by pipeline FrameCapture (substream pipe-drain at YOLO rate)
- **Doc:** `01-hardware.md:102`
- **Code reality:** Decode (and Hailo detection) now run in a supervised child process with a shared-memory ring; the old in-process ffmpeg pipe-drain FrameCapture is only the PIPE (bird_pipeline_v3.py:149 'from pipeline.frame_capture_proc import make_frame_capture as FrameCapture'; frame_ca)
- **Fix applied:** Table cell rewritten: supervised decode+detect child process (frame_capture_proc.py), PIPELINE_DECODE_INPROC=1 reverts

### 10. See the rationale in pipeline/frame_capture.py (implying that module is the live decode pa
- **Doc:** `01-hardware.md:105`
- **Code reality:** Rationale text still lives there, but live decode is PyAV inside the supervised child frame_capture_proc.py (also unpaced) (frame_capture.py:17 rationale intact; frame_capture_proc.py:13-14 'run PyAV in a CHILD process')
- **Fix applied:** Added parenthetical: decode now runs via PyAV in the supervised child, likewise native-rate

### 11. PI_MODE=1 gates the process-isolated decode child
- **Doc:** `02-services.md:35`
- **Code reality:** The decode child runs regardless of PI_MODE; only Hailo detection inside the child is PI_MODE-gated (hef_path passed iff PI_MODE and isolated) (bird_pipeline_v3.py:285-294 'hef_path=(hef_path if (PI_MODE and isolated) else None)'; make_frame_capture used)
- **Fix applied:** Reworded: PI_MODE gates Hailo detection incl. in-child; decode child itself runs regardless, PIPELINE_DECODE_INPROC=1 reverts

### 12. Current architecture (2026-05-11): FrameCapture and HailoDetector run in-process, MotionGa
- **Doc:** `03-pipeline.md:5-65 (data flow header + diagram)`
- **Code reality:** Since 2026-07-03 decode AND Hailo detection run in a supervised child process (frame_capture_proc.py, shared-memory ring, PrecomputedDetector shim, PIPELINE_DEC (bird_pipeline_v3.py:149,285-315; frame_capture_proc.py:368-389; process_thread.py:110-111)
- **Fix applied:** Added prominent UPDATE 2026-07-03 note after the heading pointing to docs/working/progress/2026-07-03-native-crash-isolation.md; heading amended

### 13. One in-process PyAV decoder per camera; the _watchdog is the production restart mechanism
- **Doc:** `03-pipeline.md:75-105 (frame capture section)`
- **Code reality:** Production default is FrameCaptureProc (child process, 6-slot ring, supervisor respawn on death or >10s stall); the in-process FrameCapture is the PIPELINE_DECO (frame_capture_proc.py:62-63,165-350,380-389)
- **Fix applied:** Added 2026-07-03 note; labeled the watchdog paragraph 'in-process fallback path'; described _supervise (CHILD_STALL_S=10)

### 14. MOG2 motion gate runs per frame and gates YOLO
- **Doc:** `03-pipeline.md:98-100 (motion gate)`
- **Code reality:** HailoDetector.uses_motion_regions=False → process_thread never calls motion_gate.regions() on the Hailo path (hailo_detector.py:43; process_thread.py:110-111)
- **Fix applied:** Added 'Bypassed on the Hailo path (2026-06-29)' paragraph

### 15. (implicit via diagram) detector runs in the parent pipeline process
- **Doc:** `03-pipeline.md:106 (Hailo detector location)`
- **Code reality:** In production HailoDetector is constructed inside the decode child; detections ride the ring; Hailo classifiers can't load in the parent meanwhile (frame_capture_proc.py:85-88,131-148,291-301; bird_pipeline_v3.py:312-315)
- **Fix applied:** Added 'Where it runs (2026-07-03)' paragraph

### 16. Module 'pipeline/tracker.py / pipeline/bird_tracker.py'; distance threshold 2.0
- **Doc:** `03-pipeline.md:122 (tracker section)`
- **Code reality:** pipeline/bird_tracker.py does not exist (bird_tracker.py is a legacy root-level file); production threshold is 2.5 via PIPELINE_TRACK_DIST, hit_counter_max 150, (pipeline/ listing; bird_pipeline_v3.py:306-310; tracker.py:24-25,133-147)
- **Fix applied:** Fixed module path; documented 2.5/150/2 env-tunables, PIPELINE_TRACK_MAX_JUMP_MULT/DEDUP_IOU, and the coasting flag

### 17. Lock at ≥3 votes AND ≥0.35 conf AND ≥60% agreement; MAX_CLASSIFICATION_ATTEMPTS = 5
- **Doc:** `03-pipeline.md:124-128 (vote-lock gate)`
- **Code reality:** Lock gate is ≥0.70 CALIBRATED P(correct) (PIPELINE_LOCK_CONF; pipeline/calibration.py isotonic map); attempts cap is 12 (PIPELINE_MAX_CLASS_ATTEMPTS); vote-elig (process_thread.py:32,336-338; classifier.py:20; pi_classifier.py:77-87; bird_pipeline_v3.py:232-233)
- **Fix applied:** Rewrote the four bullets to current values, added the floor bullet and calibration reference, and added the known ID-switch label-riding defect qualifier

### 18. queue-fed (maxsize=32, drop-oldest)
- **Doc:** `03-pipeline.md:142 (snapshot queue)`
- **Code reality:** On overflow the NEW submission is dropped and counted as dropped_full; nothing is evicted (snapshot_writer.py:233-237 ('Full — drop silently but count'))
- **Fix applied:** Changed to 'on overflow the new submission is dropped and counted as dropped_full'

### 19. wall_time_ms stamped at pipe-read in frame_capture._pipe_drain (line 147); segments.json s
- **Doc:** `03-pipeline.md:185-187 (two timestamps)`
- **Code reality:** _pipe_drain no longer exists in frame_capture.py; wall stamp happens in frame_capture_proc._child_main ring meta (or _handle_frame in-process); HlsRecorder is n (frame_capture_proc.py:147; frame_capture.py:210; bird_pipeline_v3.py:348; hls_segmenter.py:53-79)
- **Fix applied:** Rewrote both bullets; noted pts/seq/emit_ms as the sync-relevant fields

### 20. Live view uses SSE labels with CSS-driven visual smoothing
- **Doc:** `03-pipeline.md:189 (live view smoothing)`
- **Code reality:** Video-clock overlay engine (2026-07-02): rVFC-anchored camera clock, buffered SSE playout at video pace, coasting rendered dashed/dim; transform CSS transitions (CLAUDE.md Video Path section; dashboard/pi_dash.html engine; docs/working/specs/2026-07-02-overlay-video-clock)
- **Fix applied:** Replaced 'CSS-driven visual smoothing' with the video-clock engine description

### 21. Hi-res ring buffer is default-on and the active snapshot mechanism (item 3, lever #1, leve
- **Doc:** `03-pipeline.md:203-310 (strategic section, multiple spots)`
- **Code reality:** Ring is legacy (SnapshotWriter requires hires_ring=None); hi-res recovery is PTS-indexed HLS segment extraction; per RC3 the lock-time vote stays canonical and  (snapshot_writer.py:137,151,437-498,578-594; bird_pipeline_v3.py:186-189)
- **Fix applied:** Added superseded-mechanism notes at lever #1 and item 3, fixed the most misleading specifics (pts matching, RC3 metadata, hires_hls_miss counters, segmenter-ali

### 22. lock_timeouts is stuck at 0 but still reported on Pi
- **Doc:** `03-pipeline.md:222,315 (lock_timeouts)`
- **Code reality:** PiClassifier.stats has no lock_timeouts key at all (only SmartClassifier in pipeline/classifier.py has it; health.py checks it defensively) (pi_classifier.py:63-65; classifier.py:71,85,182; health.py:99)
- **Fix applied:** Both spots now say the key is absent from the Pi payload, absence is structural

### 23. feeder-sub and feeder-main pulled as two ffmpeg subprocesses against go2rtc
- **Doc:** `03-pipeline.md:219 (stream pulls)`
- **Code reality:** Both are PyAV decoders — substream in the supervised decode child, main stream in HlsSegmenter's passthrough demux (frame_capture_proc.py:110-122; hls_segmenter.py:1,270)
- **Fix applied:** Rewrote the sentence

### 24. proc.poll() short-circuit lives in both frame_capture.py:166-185 and hires_ring.py:238-282
- **Doc:** `03-pipeline.md:281,318 (proc.poll fix location)`
- **Code reality:** Only hires_ring.py retains it; frame_capture.py's PyAV rewrite removed the subprocess; the current-shape equivalent is frame_capture_proc._supervise (child-deat (frame_capture.py (no poll); hires_ring.py:282; frame_capture_proc.py:317-350)
- **Fix applied:** Both lever #6 and watch-out #2 updated; noted reader_restarts_last_hour alias

### 25. hires_skipped < 5%
- **Doc:** `03-pipeline.md:359 (targets bullet)`
- **Code reality:** No hires_skipped counter; the fallback counter is hires_fail (with hires_hls_miss as the HLS-specific miss) (snapshot_writer.py:162-176)
- **Fix applied:** Changed to hires_fail and 'segmenter mid-restart'

### 26. pauses ~30 minutes after sunset and resumes at sunrise
- **Doc:** `03-pipeline.md:305 (nighttime pause)`
- **Code reality:** Resumes ~30 minutes BEFORE sunrise (sunrise_cutoff = sunrise_local - offset_minutes/60) (solar_utils.py:78-80)
- **Fix applied:** Changed to '~30 minutes before sunrise'; added PIPELINE_NIGHT_BYPASS mention

### 27. feeder_*.jpg currently 640×360 — see 'snapshot regression' below
- **Doc:** `03-pipeline.md:60-62 (diagram output box)`
- **Code reality:** The regression was resolved (the doc's own later section says so); snapshots are 1080p via HLS-by-PTS recovery (snapshot_writer.py:437-471)
- **Fix applied:** Diagram box now reads '1080p — recovered from HLS by PTS at lock time'

### 28. distance threshold of 2.0, parameter at tracker.py line 84 pinned to 2.0
- **Doc:** `03-pipeline.md:373-381 (tracker threshold section)`
- **Code reality:** 2.5 via PIPELINE_TRACK_DIST (class default 2.0 in BirdTracker.__init__, tracker.py:133), plus the 2026-07-01 absolute jump ceiling (bird_pipeline_v3.py:307; tracker.py:24,120-127,133)
- **Fix applied:** Updated values, line reference, added jump-ceiling sentence and Blue Jay qualifier; lever #4 heading and 'Document why 2.0' bullet updated to 2.5

### 29. The engine/VDevice lives in the (one) pipeline process; Hailo detector + classifier cohabi
- **Doc:** `04-hailo-engine.md (doc-wide: intro, section #6, Lifecycle, Future Tier 2)`
- **Code reality:** Since 2026-07-03 the live HailoDetector (and the VDevice) runs in the supervised decode+detect CHILD process; parent uses PrecomputedDetector via a shared-memor (pipeline/frame_capture_proc.py:31-43,85-88,205 ('never fork a Hailo/onnx parent'); bird_pipeline_v3.py:284-319)
- **Fix applied:** Added prominent note after the intro pointing to docs/working/progress/2026-07-03-native-crash-isolation.md; appended caveat to section #6 ('one process' = capt

### 30. Pipeline target throughput is 5 FPS on the substream; ~9x detector headroom; 'steady 5 FPS
- **Doc:** `04-hailo-engine.md:77,115,151,185`
- **Code reality:** Detection runs full-frame on EVERY substream frame (~30 FPS) since the 2026-06-29 foundations work; headroom vs 45.5 FPS co-scheduled is ~1.5x (frame_capture_proc.py:122-149 (detect per decoded frame); bird_pipeline_v3.py:301 '@30fps'; docs/working/specs)
- **Fix applied:** All four occurrences updated to ~30 FPS full-frame; headroom corrected to ~1.5x in both places

### 31. We register a SIGTERM handler in bird-pipeline.service to do this [CIM drain + release] gr
- **Doc:** `04-hailo-engine.md:175 (Cleanup discipline #7)`
- **Code reality:** SIGTERM handler exists (bird_pipeline_v3.py:165) but only sets running=False and stops captures/servers; nothing on the live path calls HailoEngine.shutdown() — (bird_pipeline_v3.py:108-111,440-457 — no HailoEngine.shutdown() call anywhere in the file)
- **Fix applied:** Sentence rewritten: sequence implemented in HailoModel.close()/HailoEngine.shutdown() but not invoked on the live shutdown path; callers named

### 32. HailoEngine.shutdown() ... Called by bird_pipeline_v3.py on shutdown signal
- **Doc:** `04-hailo-engine.md Lifecycle bullet 3`
- **Code reality:** bird_pipeline_v3.py never calls it; the Hailo-owning capture child simply exits (rg over repo: shutdown() callers are tests, _reset_for_testing, bench_hailo_multimodel.py:114)
- **Fix applied:** Bullet corrected to 'Not called on the live shutdown path today' with actual callers

### 33. tests verify ... format-type configuration
- **Doc:** `04-hailo-engine.md Testing-without-HW section`
- **Code reality:** No test asserts set_format_type; named tests cover singleton-ness, per-path caching, shutdown/release, and the flat-output parser (rg 'set_format_type' tests/ -> no hits; test names at test_hailo_engine.py:73-114, test_hailo_detector_engine.)
- **Fix applied:** Replaced 'format-type configuration' with 'VDevice release on shutdown'

### 34. Today: 83-85 °C with active fan at full RPM
- **Doc:** `04-hailo-engine.md 'What good looks like' thermal bullet`
- **Code reality:** ~69 °C steady-state since the 2026-06-29 MOG2-bypass fix (the hog was wasted per-frame MOG2, not the NPU) (docs/working/specs/2026-06-27-pi-live-id-foundations-design.md:42 (84°C -> target ~68-72°C); hailo_detector.py)
- **Fix applied:** Bullet updated to ~69 °C current with the old figure kept as historical context

### 35. LAN transport loads video-stream.js and WS directly from pi5.local:1984 (go2rtc), tunnel g
- **Doc:** `05-dashboard.md:24`
- **Code reality:** Both LAN and tunnel are same-origin: script from /video-stream.js and WS to /api/ws on the dashboard host, proxied to go2rtc (pi_dash.html:1351 (go2rtcWs = window.location.host), 1358 (script.src='/video-stream.js?...'), 1393)
- **Fix applied:** Rewrote as 'One same-origin transport for both network paths' with LAN+tunnel merged bullet

### 36. reconnectLiveVideo(next) detaches and reattaches the <video-stream> element
- **Doc:** `05-dashboard.md:26`
- **Code reality:** It tears down the WS/PeerConnection session via video.ondisconnect() and assigns the new src; guard is next===currentSrc early-return (pi_dash.html:1390-1411)
- **Fix applied:** Reworded to session teardown + src assign with the currentSrc guard

### 37. Smoothing = CSS transition: transform 240ms cubic-bezier on bbox/label nodes, browser inte
- **Doc:** `05-dashboard.md:28`
- **Code reality:** Superseded 2026-07-02: transform transitions removed ('NO transform/size transitions' in CSS, opacity only); video-clock engine buffers events per-track by pts, (pi_dash.html:310-313, 1459-1501, 1661, 1858-1941)
- **Fix applied:** Replaced bullet with supersession note pointing at 2026-07-02 spec + accurate engine summary

### 38. Track GC after 1.5 s without an update
- **Doc:** `05-dashboard.md:29`
- **Code reality:** Death fade at 0.6 s staleness in video time (300 ms fade), 15 s wall-clock backstop; the 1.5 s arrival-time GC applies only while the clock is uncalibrated (pi_dash.html:1859-1860 (STALE_TRACK_S=0.6, BACKSTOP_GC_MS=15000), 1891-1896)
- **Fix applied:** Updated lifecycle bullet with the three timings + coasting dashed/dim note

### 39. Viewport resilience via window resize listener reflowing every bbox (rAF-coalesced)
- **Doc:** `05-dashboard.md:30`
- **Code reality:** ResizeObserver on #live-stage refreshes a cached rect; the rAF render loop re-applies geometry next frame — no resize listener, no per-track reflow handler (pi_dash.html:1737-1742, 2234-2236 ('No per-track work needed here anymore'))
- **Fix applied:** Reworded to ResizeObserver + rAF loop

### 40. Telemetry counters expose ws/sse reconnect counts
- **Doc:** `05-dashboard.md:31`
- **Code reality:** No reconnect counters exist; window.__overlayDebug exposes sseCount, seqGaps, and est.reanchors instead (pi_dash.html:2163-2207 — no 'reconnect' counter fields; rg 'reconnect' finds none)
- **Fix applied:** Replaced with '__overlayDebug exposes event counts, seq gaps, and clock re-anchor counts'

### 41. POST /api/demo-mode starts/stops bird-demo-loop.service, sets/unsets PIPELINE_TEST_RTSP_UR
- **Doc:** `05-dashboard.md:41`
- **Code reality:** No service start/stop: sets/unsets PIPELINE_TEST_RTSP_URL=rtsp://127.0.0.1:8554/feeder-demo plus PIPELINE_DISABLE_SEGMENTER/PIPELINE_DISABLE_SNAPSHOTS/PIPELINE_ (api.py:5334-5367, 5312-5319; tools/sim_set.sh)
- **Fix applied:** Rewrote the toggle bullet with the actual env vars and one-producer-one-clock note; also fixed 'relay of the demo loop' → 'local demo loop' and 'next to the mod

### 42. BirdAPIRewriteMiddleware in dashboard/api.py:63
- **Doc:** `05-dashboard.md:87`
- **Code reality:** Class is at api.py:70 (api.py:70)
- **Fix applied:** Line number updated to 70

### 43. Crop bbox pulled from classifications.db extra_json.best_detection.box
- **Doc:** `05-dashboard.md:79`
- **Code reality:** bbox comes from the best_detection_json column (via cdb.get_entry_by_file → entry['best_detection']['box']); extra_json is a separate column (api.py:1819-1822; classifications_db.py:90, 102, 260)
- **Fix applied:** Changed parenthetical to 'the row's best_detection_json box'

### 44. API return shapes: POST {ok,file,verdict,model_source}; DELETE {ok,file,deleted}; recent i
- **Doc:** `06-pi-review.md:24-27`
- **Code reality:** POST/DELETE also return source_mode; recent items have NO source_mode field — the mode is a top-level response key; stats also returns top-level mode (pi_review.py:140-146, 161, 214-219, 262-267)
- **Fix applied:** Table updated to exact return shapes

### 45. Two SQLite verdict files, one per mode (sibling demo file, same schema)
- **Doc:** `06-pi-review.md:33-36`
- **Code reality:** Single ~/bird-snapshots/logs/pi_reviews.db for both modes, scoped by the source_mode column; only the classifications DBs are split into two files (pi_review.py:31 (single DB_PATH), 128-139/156/207/237 (source_mode-filtered queries))
- **Fix applied:** Rewrote to 'One SQLite verdicts file, mode-scoped by column'

### 46. Schema block: model_source before source_mode, two indexes
- **Doc:** `06-pi-review.md:42-52`
- **Code reality:** Column order is file, verdict, reviewed_at, source_mode, model_source; there is a third index idx_pi_reviews_source_mode (pi_review.py:87-99)
- **Fix applied:** Schema block updated to match code exactly; 'Single table per file' → 'Single table'

### 47. dashboard/pi_review.py — the whole module (227 lines)
- **Doc:** `06-pi-review.md:80`
- **Code reality:** Module is 267 lines (wc -l dashboard/pi_review.py → 267)
- **Fix applied:** 227 → 267

### 48. Mount code at dashboard/api.py:98–108
- **Doc:** `06-pi-review.md:81`
- **Code reality:** PI_MODE-gated mount block is at api.py:103–115 (api.py:103-115)
- **Fix applied:** Range updated to 103–115

### 49. Hailo YOLO every motion-gate-passing frame
- **Doc:** `07-thermal.md:11`
- **Code reality:** The Hailo path never consumed motion regions — YOLO runs full-frame on every frame (pipeline/hailo_detector.py:43 uses_motion_regions=False; pipeline/process_thread.py:110-111 skips regions for )
- **Fix applied:** Rewrote parenthetical to 'Hailo YOLO full-frame on every frame — the Hailo path never consumed the motion gate's regions'

### 50. Ring-buffer fps is a knob; hi-res ring currently decodes main stream at 5 fps — easiest si
- **Doc:** `07-thermal.md:56`
- **Code reality:** Ring is legacy: SnapshotWriter requires hires_ring=None; 1080p comes from HLS-segment demux by PTS (pipeline/snapshot_writer.py:133-151 — "hires_ring: legacy, must be None"; :317 segments.json demux)
- **Fix applied:** Retitled 'was a knob — retired', past-tensed the filter description, pointed at the HLS-by-PTS path

### 51. The CSV row tells you what the pipeline was doing (incl. ring_pick_ok/ring_pick_empty)
- **Doc:** `07-thermal.md:37`
- **Code reality:** snapshot_writer stats no longer publish ring_pick_* — those two fields are empty on new rows (pipeline/snapshot_writer.py:163-164 stats = submitted/written only; pi5_thermal_watch.py:139-140 reads missing)
- **Fix applied:** Appended a parenthetical marking ring_pick_ok/ring_pick_empty as legacy, always-empty columns

### 52. Split context at docs/working/progress/2026-04-25-pi-repo-split.md
- **Doc:** `08-deployment.md:15`
- **Code reality:** File lives at docs/historical/2026-04-25-pi-repo-split.md (find: ./docs/historical/2026-04-25-pi-repo-split.md; not present under docs/working/progress/)
- **Fix applied:** Path corrected to docs/historical/

### 53. Pi-only modules include pi_classifier.py and model_registry.py (bare paths)
- **Doc:** `08-deployment.md:41`
- **Code reality:** Both live under pipeline/ (pipeline/pi_classifier.py, pipeline/model_registry.py exist; no top-level copies)
- **Fix applied:** Prefixed both with pipeline/

### 54. ~/.bird-observatory-env holds UNIFI_API_KEY, PI_CLASSIFIER, PIPELINE_HIRES_RING
- **Doc:** `08-deployment.md:51`
- **Code reality:** No code reads PIPELINE_HIRES_RING anymore (only historical docs mention it); other PIPELINE_* overrides are live (rg PIPELINE_HIRES_RING across *.py → zero hits; UNIFI_API_KEY tools/refresh_rtsp.py:39, PI_CLASSIFIER pipeline)
- **Fix applied:** Replaced with 'PIPELINE_* tuning overrides; PIPELINE_HIRES_RING is retired — nothing reads it anymore'

### 55. Snapshot should be 1920x1080 with PIPELINE_HIRES_RING=authoritative
- **Doc:** `08-deployment.md:71`
- **Code reality:** 1080p comes from SnapshotWriter's HLS-segment demux by PTS; the env var is dead (pipeline/snapshot_writer.py:317,390 segments.json lookup; no PIPELINE_HIRES_RING reads in code)
- **Fix applied:** Comment now credits the HLS-by-PTS demux

### 56. Boot architecture (current): SD=bootloader+kernel, NVMe=rootfs; RTL9210 can't serve bootlo
- **Doc:** `08-deployment.md:118-123`
- **Code reality:** Superseded 2026-06-27: ASMedia ASM2364 enclosure, direct NVMe boot, root=/dev/birdroot + initramfs hook, SD as rescue OS with auto-recovery watcher (No boot tooling in code repo; sibling chapter 01-hardware.md:11,20 already records the supersession (hardware-)
- **Fix applied:** Added prominent SUPERSEDED note at section top pointing to 01-hardware.md; body kept as the historical RTL9210-era record (summarized entry per protocol)

### 57. There's no software path to recover from the page-cache zombie
- **Doc:** `08-deployment.md:142`
- **Code reality:** Root-level service-canary restarts dashboard/sshd and escalates to reboot; wd-disk-canary feeds the hardware watchdog an O_DIRECT disk probe (deploy/systemd/service-canary.sh:1-28 (3-fail restart, 6-fail reboot); deploy/systemd/wd-disk-canary.sh)
- **Fix applied:** Qualified: canary self-heal described, manual power-cycle kept as fallback

### 58. Three confirmed zombie instances; enclosure replacement pending (Pineboards leading candid
- **Doc:** `08-deployment.md:145`
- **Code reality:** 01-hardware.md counts four May-June incidents; enclosure swapped 2026-06-27 to ASMedia ASM2364; Pineboards is the future-build candidate (01-hardware.md:11 ("enclosure swapped to an ASMedia ASM2364 ... 2026-06-27"), :44, :129)
- **Fix applied:** Updated count reference and replaced 'pending' with the completed ASM2364 swap

### 59. Camera ingestion is pipeline/frame_capture.py (named as the current ingestion module)
- **Doc:** `09-the-unified-brain.md "Camera ingestion" heading (was line 99)`
- **Code reality:** Since 2026-07 decode (and Hailo detection) run in a supervised child process, pipeline/frame_capture_proc.py; frame_capture.py is the wrapped decoder / PIPELINE (pipeline/frame_capture_proc.py:1-45 — "run PyAV in a CHILD process... Same public surface as pipeline.frame_ca)
- **Fix applied:** Added parenthetical: "since 2026-07 hosted inside the supervised decode child pipeline/frame_capture_proc.py"; rest of para untouched

### 60. Flagship deployment = drop HEF in ~/bird-classifier-pi/models/
- **Doc:** `09-the-unified-brain.md "What the migration does NOT change" Tier 2 bullet (was line 119)`
- **Code reality:** The model registry discovers Hailo HEFs at /usr/share/hailo-models/ (hailo_root); repo models/ holds ONNX/tflite CPU models (pipeline/model_registry.py:200 — hailo_root = Path("/usr/share/hailo-models"); availability = HEF presence the)
- **Fix applied:** Changed path to /usr/share/hailo-models/ with "where the model registry discovers HEFs"

### 61. go2rtc.vivessato.com is its own subdomain on its own tunnel (WebRTC TURN config to not mis
- **Doc:** `09-the-unified-brain.md watch-out #3 (was line 256)`
- **Code reality:** Retired as a public hostname on the Pi side — the dashboard proxies go2rtc over its own /api/ws; a test asserts the hostname is absent from served HTML (dashboard/index.html:3494 "This retires go2rtc.vivessato.com as a public hostname"; tests/test_dashboard_live_)
- **Fix applied:** Rewrote parenthetical: Pi proxies via /api/ws, subdomain retired on Pi side, iMac may still carry it

### 62. Repo-split doc lives at ~/bird-classifier-pi/docs/working/progress/2026-04-25-pi-repo-spli
- **Doc:** `09-the-unified-brain.md cross-system section + references (was lines 336, 398)`
- **Code reality:** File moved to docs/historical/2026-04-25-pi-repo-split.md (no working/progress copy exists) (find: only /Users/vives/bird-classifier-pi/docs/historical/2026-04-25-pi-repo-split.md exists)
- **Fix applied:** Updated both references to docs/historical/ path (replace_all)

### 63. Live view smoothing = CSS transition: transform 240ms cubic-bezier; avenue #1 status LIVE
- **Doc:** `10-overlay-sync.md:34 (status update #1) + avenue #1`
- **Code reality:** Positional CSS transitions removed 2026-07-02; rAF video-clock engine positions labels per displayed frame, opacity transitions only (pi_dash.html:310-313 'NO transform/size transitions... Opacity only', :1626 requestVideoFrameCallback)
- **Fix applied:** Added parenthetical to status-update item 1; avenue #1 status → SUPERSEDED (2026-07-02) with video-clock pointer

### 64. bird_pipeline_v3.py:246 passes CAMERAS_DETECT[name]
- **Doc:** `10-overlay-sync.md:35 ('What replaced it' #2)`
- **Code reality:** The line moved to 266 (bird_pipeline_v3.py:266 'detect_url = CAMERAS_DETECT[name]')
- **Fix applied:** Updated line ref 246 → 266

### 65. /api/hls-live route at dashboard/api.py:282
- **Doc:** `10-overlay-sync.md:54 (bedrock list)`
- **Code reality:** Route decorator is at line 283 (dashboard/api.py:283)
- **Fix applied:** Updated 282 → 283

### 66. Live path does no PTS math; ~200-500ms label lag is the accepted trade
- **Doc:** `10-overlay-sync.md:64 ('The principle still holds', live-path bullet)`
- **Code reality:** Video-clock engine matches labels to displayed frames by camera clock (rVFC rtpTimestamp/mediaTime), rig-certified +5ms (pi_dash.html:1584-1614 rtpTimestamp/mediaTime anchors, :1461-1479 clock estimator)
- **Fix applied:** Appended supersede parenthetical pointing to top note

### 67. Frame-accurate live sync still unsolved; Codex spatial-subtitle spec (8-12s buffer) deferr
- **Doc:** `10-overlay-sync.md:71 ('What hasn't been solved' #1) + avenue #6`
- **Code reality:** Solved 2026-07-02 by the video-clock engine with ~0.8s jitter buffer, not 8-12s (pi_dash.html:1661 jitterBufferTarget = 800; spec docs/working/specs/2026-07-02-overlay-video-clock-sync-design)
- **Fix applied:** Item 1 marked SOLVED 2026-07-02; avenue #6 → SUPERSEDED with spec pointer

### 68. Demo mode = bird-demo-loop.service publishing to mediamtx :8654, go2rtc relays it
- **Doc:** `10-overlay-sync.md: avenue #3`
- **Code reality:** feeder-demo is now a local ffmpeg exec loop of ~/sim/current.mp4; POST /api/demo-mode points the pipeline at rtsp://127.0.0.1:8554/feeder-demo with segmenter/sn (dashboard/api.py:5312-5319 'local ffmpeg exec loop of ~/sim/current.mp4', :5334-5368 POST handler; tools/sim_s)
- **Fix applied:** Rewrote avenue #3 notes cell; status LIVE (reworked 2026-07-02)

### 69. Frame-accurate live without long delay over WebRTC is DEAD — no transport exposes the came
- **Doc:** `10-overlay-sync.md: avenue #11`
- **Code reality:** rVFC metadata exposes it per displayed frame (WebRTC rtpTimestamp, MSE mediaTime); shipped and rig-certified +5ms / 3.2px (pi_dash.html:1584-1621 rtpTimestamp/mediaTime anchoring, :1626 rVFC loop)
- **Fix applied:** Status → LIVE (2026-07-02 — was DEAD, verdict overturned) with explanation

### 70. HLS+canvas live view, single shared decoder for detection+segmenting, hls.js one-transport
- **Doc:** `10-overlay-sync.md: entire 2026-05-10 preserved chapter (lines ~84-277)`
- **Code reality:** Superseded twice: 2026-05-11 revert to WebRTC+DOM, then 2026-07-02 video-clock engine; segmenter uses a separate main-stream client and 2s segments (pi_dash.html:804-808 WebRTC video-stream element; bird_pipeline_v3.py:265-266 two sources; hls_segmenter.py:19)
- **Fix applied:** Superseded-section rule: prominent note added at chapter top naming the 2026-07-02 spec + certified numbers; preserved-prose intro line now points to it; prose 

### 71–84. Repo living docs (hand-audited, main session)
- ROADMAP.md: Ch-1 status 🔴→🟡 w/ certified numbers + named residuals; NAS-demo block superseded (split-brain reversal, measured); offset band ±40 ms achieved (+5 ms); off-Pi-feed constraint struck with rationale; overlay-stack line → video-clock engine; single-coder ownership; Ch-1 sequence marked COMPLETE with as-built details.
- GUIDE.md + docs/README.md: chapters 00→10; handoff line reframed; sync-spec + crash-isolation pointers added; cross-claude bus marked historical.
- CLAUDE.md: pipeline chain → supervised decode+detect child; lock gate → calibrated ≥0.70; services header → full set in deploy/systemd/.

---

## ❌ Hallucination (auto-fixed)

### 1. Hi-res ring buffer is default-on via env var PIPELINE_HIRES_RING=authoritative, which requ
- **Doc:** `03-pipeline.md:203,323 (formerly 'PIPELINE_HIRES_RING=authoritative')`
- **Verification attempts:** rg 'PIPELINE_HIRES_RING' across repo — zero hits in pipeline/dashboard Python code; only dashboard/work.html (historical planning notes) mentions it; no os.environ read anywhere; SnapshotWriter hard-r
- **Fix applied:** Removed the env-var claim; item 3 now carries a superseded-mechanism note naming the HLS-by-PTS path; watch-out #7 rewritten around HlsSegmenter liveness and hi

### 2. hires_skipped counter already exposed at /api/pipeline/health
- **Doc:** `03-pipeline.md:286 (formerly 'hires_skipped counter')`
- **Verification attempts:** rg 'hires_skipped' across repo — only dashboard/work.html historical notes; snapshot_writer.py stats dict has hires_ok/hires_fail/hires_inline_ok/hires_hls_ok/hires_hls_miss/hires_lowres_fallback, no 
- **Fix applied:** Replaced with the real counters (hires_hls_miss, hires_fail) in lever #7, watch-out #7, and the targets bullet

---

## 🐛 Smells (flagged for human review — outcomes as of 2026-07-03 EOD)

### 1. Checked-in go2rtc.yaml still defines feeder-demo as a relay of the retired mediamtx loop (rtsp://loc — confidence: medium
- **Code:** `go2rtc.yaml:16-17 (also tools/refresh_rtsp.py:121 comment)`
- **Why suspicious:** If this yaml is what go2rtc actually loads, demo mode's video source is a dead endpoint (mediamtx/:8654 no longer runs) and the demo toggle shows a black stream
- **Triggered by doc claim:** 02-services.md supersession note: 'go2rtc's feeder-demo stream is now a local ffmpeg exec loop of ~/sim/current.mp4'
- **Outcome:** FIXED 2026-07-03: tracked .bak/.example files scrubbed to placeholders (leaked tokens were stale/rotated; LAN-only); local feeder-demo entry updated to the live exec loop.

### 2. submit() docstring says 'Drops oldest on backpressure' but the code drops the NEW payload on queue.F — confidence: high
- **Code:** `pipeline/snapshot_writer.py:196 (docstring) vs :233-237 (implementation)`
- **Why suspicious:** Under sustained lock bursts the freshest locked tracks are the ones silently lost, the opposite of the documented (and diagram-implied) drop-oldest policy used 
- **Triggered by doc claim:** Doc claim 'Background thread, queue-fed (maxsize=32, drop-oldest)'
- **Outcome:** FIXED 2026-07-03: docstring corrected (drops the NEW payload, counted).

### 3. Once is_locked=True the species label rides the Norfair track_id forever with no re-verification; an — confidence: high
- **Code:** `pipeline/process_thread.py:336-341 (lock) + pipeline/tracker.py:249-278 (ID-switch detection)`
- **Why suspicious:** Breaks the doc's core promise that a fired lock means the species on-screen is right; id_switches are counted but never invalidate the lock
- **Triggered by doc claim:** Doc claims 'Per-track precision (when the lock fires, is the species right?)' and vote-lock as the acceptance criterion
- **Outcome:** OPEN — tasked (the Blue Jay defect): post-lock re-verification + tracker ReID, David to prioritize.

### 4. With PrecomputedDetector, det_ms measured in process_thread times the no-op shim (~0 ms) and feeds y — confidence: high
- **Code:** `pipeline/process_thread.py:120-131 + pipeline/frame_capture_proc.py:368-377`
- **Why suspicious:** The honesty-contract metrics yolo_ms_avg/p99 at /api/pipeline/health now report near-zero regardless of actual NPU latency — exactly the 'p99-lying' failure mod
- **Triggered by doc claim:** Doc claims yolo_ms_avg/yolo_ms_p99 are honesty-contract detector-latency metrics on /api/pipeline/health
- **Outcome:** ALREADY FIXED pre-report (commit 9304013): det_ms now read from frame.det_ms; live yolo_ms_avg 18 ms.

### 5. The documented graceful Hailo cleanup (CIM drain -> shutdown -> __exit__ -> vdevice.release) is neve — confidence: medium
- **Code:** `pipeline/frame_capture_proc.py:238-244,65 + bird_pipeline_v3.py:440-457`
- **Why suspicious:** A stall-killed child could leave the kernel driver holding the device busy and the respawned child could fail VDevice creation in a tight retry loop, extending 
- **Triggered by doc claim:** Doc claims 'We register a SIGTERM handler ... to do this gracefully' (Cleanup discipline #7) and 'HailoEngine.shutdown()
- **Outcome:** NOTED — graceful Hailo teardown path unreached; harmless (child is killed by supervisor); revisit with segmenter caging.

### 6. Dashboard-facing `info` strings still say 'the pipeline's 5 FPS detection rate' and 'sub-stream is a — confidence: high
- **Code:** `pipeline/model_registry.py:221-222,294-295`
- **Why suspicious:** Users reading the Model Lab lightbox get a wrong picture of the live pipeline's rate; spawned a background task chip (task_b9ef42bb) to fix since code is read-o
- **Triggered by doc claim:** Doc claim that `info` is 'the multi-paragraph deep-dive shown in the dashboard's per-model lightbox'
- **Outcome:** IN PROGRESS in a spawned task (task_b9ef42bb): stale 5-FPS Model-Lab info strings.

### 7. In-code comments still describe the retired 'CSS transition smoothing (240ms cubic-bezier)' live-vie — confidence: high
- **Code:** `dashboard/pi_dash.html:804-807 and 1290-1310`
- **Why suspicious:** They directly contradict the shipped video-clock engine and the CSS at line 310-313 ('NO transform/size transitions'); these stale comments are the likely sourc
- **Triggered by doc claim:** 05-dashboard.md smoothing claim 'CSS transition: transform 240ms cubic-bezier'
- **Outcome:** FIXED 2026-07-03: both stale CSS-smoothing comments rewritten to describe the video-clock engine.

### 8. The sampler reads sw.get('ring_pick_ok') / sw.get('ring_pick_empty'), but SnapshotWriter.stats only  — confidence: high
- **Code:** `tools/pi5_thermal_watch.py:139-140`
- **Why suspicious:** The doc sells the CSV as a one-view correlation of thermal + pipeline activity; two of its sixteen columns are dead weight and could be mistaken for a broken sn
- **Triggered by doc claim:** 07-thermal.md:29-37 — CSV column list and 'the row tells you what the pipeline was doing'
- **Outcome:** FIXED 2026-07-03: sampler reads hires_hls_ok/hires_hls_miss (the keys SnapshotWriter publishes).

### 9. setupLiveView() header comment still documents 'Smoothing: CSS transform transitions (240ms cubic-be — confidence: high
- **Code:** `dashboard/pi_dash.html:1305-1306`
- **Why suspicious:** Directly contradicts the implementation it heads — CSS at :310-313 says 'NO transform/size transitions... Opacity only' and the rAF video-clock engine positions
- **Triggered by doc claim:** Doc claim that live-view smoothing is CSS transform transitions (status update #1, avenue #1)
- **Outcome:** FIXED 2026-07-03 (same edit).

### 10. Repo go2rtc.yaml still wires feeder-demo to the retired mediamtx :8654 relay (bird-demo-loop), not t — confidence: medium
- **Code:** `go2rtc.yaml:11-17`
- **Why suspicious:** deploy/systemd/go2rtc.service loads %h/bird-classifier/go2rtc.yaml; if this copy is what's deployed, demo mode plays a dead relay and tools/sim_set.sh's 'go2rtc
- **Triggered by doc claim:** Avenue #3 claim that the demo-mode loop is LIVE via the feeder-demo go2rtc stream
- **Outcome:** FIXED 2026-07-03: tracked .bak/.example files scrubbed to placeholders (leaked tokens were stale/rotated; LAN-only); local feeder-demo entry updated to the live exec loop.

---

## ⏭ Skipped

- `00-overview.md:32` — Cloudflared also exposes go2rtc.vivessato.com — **reason:** Tunnel hostname routing lives in ~/.cloudflared on the Pi, outside the code root; repo evidence is only negative (tests/
- `00-overview.md:21-22` — iMac CoreML ~98 ms comparison; NVMe ~450 MB/s sustained — **reason:** Hardware/historical benchmark claims, out of scope per protocol
- `_truth-staging-2026-06-11.md (whole file)` — Jun-12 incident narrative, zombie theory revision, enclosure order — **reason:** Dated addendum / staging record (changelog-style), not current-state prose; its checkable deploy-side component claims a
- `README.md:20 / 00-overview.md:57` — Ch. 09 Unified Brain architecture — **reason:** Explicitly labeled "destination, not what runs today" — aspirational
- `README.md:51-53` — Chapters authored in the 2026-04-26 doc audit pass, verified against live code — **reason:** Historical record of a past audit, not a current-state claim (DOC_AUDIT.md existence itself verified)
- `01-hardware.md:7-16 (inventory table)` — Pi 5 RAM/PSU/fan/camera/OS/kernel/HailoRT+hailo-all versions — **reason:** Hardware/third-party host state, not verifiable from the repo
- `01-hardware.md:18-68 (boot architecture + runtime drop)` — EEPROM BOOT_ORDER, PARTUUIDs, RTL9210 bootloader/zombie behavior — **reason:** Pi-host/bootloader state, explicitly marked SUPERSEDED/historical in the doc itself
- `01-hardware.md:107-141 (thermal + NVMe throughput)` — ~69°C sustained, 80°C soft-throttle, ~450/400 MB/s, 7.4 ms/crop AIY — **reason:** Runtime/hardware performance metrics, not statically verifiable (consistent with CLAUDE.md)
- `02-services.md:12` — Cloudflared tunnel UUID bf725288-… and ~/.cloudflared/config.yml — **reason:** Pi-host infra config, not present in repo (unit just runs 'cloudflared tunnel run')
- `02-services.md:3,52` — loginctl enable-linger set; journald writes /var/log/journal on the NVMe — **reason:** Host/OS state, not verifiable from repo
- `02-services.md:86-100 (bird-demo-loop section body)` — mediamtx :8654 unit, may10 demo video, Restart=on-failure — **reason:** Kept as explicit historical record under an accurate SUPERSEDED banner (unit confirmed absent from deploy/systemd/; scri
- `03-pipeline.md:79-81, 139-145 ('Historical: two-decoder ring-buffer architecture' framing)` — 2026-04-30-era two-stream design narrative and why the ring classes are kept — **reason:** Explicitly historical/rationale prose, presented as record not current state; still consistent with code (classes exist,
- `03-pipeline.md:151 (commit hash)` — commit 787810d, 2026-05-12 — **reason:** Historical changelog reference; working dir reports as non-git so hash unverifiable, and it's a dated record
- `03-pipeline.md:120-121, 340-345 (perf numbers)` — ~17 ms isolated / ~22 ms co-scheduled Hailo budget; ~6 ms scheduler overhead; ~7 — **reason:** Dated bench measurements (2026-04-25) presented as measurements; 7.4 ms is echoed in code notes (model_registry.py:216) 
- `03-pipeline.md:329-350 ('Cutting-edge research validation (2026-04-29 pass)')` — HailoRT 4.23 SOTA status, Pi AI HAT+ 26 TOPS, ByteTrack/OC-SORT findings — **reason:** Dated third-party/literature review, explicitly labeled as a dated pass
- `03-pipeline.md:352-360 ('What as good as we possibly can')` — Precision 76%→88-92%, lock latency 600ms→400ms targets — **reason:** Aspirational targets, not current-state claims (counter-name fix applied separately)
- `03-pipeline.md:240-246 (lever #2 scheduler tuning)` — set_scheduler_threshold/set_scheduler_timeout become live knobs when flagship sh — **reason:** Explicitly future/open lever; 'det only today, classifier on CPU' still matches code
- `03-pipeline.md throughout (iMac-side claims)` — Chapter 23 contents, iMac Coral lock behavior, iMac /live.html design — **reason:** Claims about the other repo/system (/Users/vives/bird-classifier/), outside this audit's code root
- `03-pipeline.md:357 ('[LIVE: pipeline.feeder.capture.ffmpeg_restarts_last_hour]')` — Live-value placeholder token — **reason:** Deliberate template placeholder, not a checkable claim; key exists in health payload (process_thread.py:392)
- `04-hailo-engine.md:3,87-93,189-205 etc.` — HailoRT/hardware behavior: error codes 74/73, one-VDevice-slot semantics, DFC x8 — **reason:** Third-party/hardware claims — not verifiable against this repo (engine code is consistent with them where it touches, e.
- `04-hailo-engine.md:66-77,109-115 (bench numbers)` — 16.97/20.97/22.0/22.6 ms, 58.9/47.7/45.5/44.2 FPS, ~6 ms pair overhead — **reason:** Dated measurements ('measured 2026-04-25') — historical records; only the current-state 5 FPS/headroom framing around th
- `04-hailo-engine.md 'Cutting-edge research validation (2026-04-28 pass)' + References` — Literature findings, Hailo-10H/15/AI HAT+ product facts, external links — **reason:** Dated third-party research record; external products and URLs are outside the code root
- `04-hailo-engine.md watchout #10 (hailo_pci find_vma WARNINGs)` — Kernel driver bug analysis, 17 warnings at t≈52 s — **reason:** Kernel/third-party driver observation with its own dated record; not checkable in repo code
- `04-hailo-engine.md 'What as-good-as-we-possibly-can looks like' targets` — 30 FPS each ceiling, sub-22 ms, 3-4 model deployment — **reason:** Aspirational ceiling targets, not current-state claims (except the thermal 'Today' line, which was fixed as drift)
- `04-hailo-engine.md cross-references to 01/03/07/09/28 chapters + shared docs` — Content of other book chapters — **reason:** Other doc files, outside this agent's assignment; existence-level pointers only
- `05-dashboard.md:3` — Tunnel gated behind Cloudflare Access; LAN at pi5.local:8099 unauthenticated — **reason:** Third-party/infra configuration, not verifiable from the code root
- `05-dashboard.md:25-31,37; 06-pi-review.md:33` — Commit hashes 4ca690e, 354c1eb, 6d5216c, 8af62b5, 0aeed01, e2d23b3, 0fd3615 and  — **reason:** Historical provenance; working directory reports no git repo, so hashes are uncheckable — behavioral content around them
- `05-dashboard.md:35` — Provenance note about the 2026-05-10/11 HLS+canvas detour and revert — **reason:** Dated historical narrative, not presented as current state (server-side bedrock claims cross-doc to 10-overlay-sync.md, 
- `05-dashboard.md:21` — iMac /live.html HLS + sidecar clock + two Gaussian kernels, ~10s delay — **reason:** iMac-side code lives outside this code root (/Users/vives/bird-classifier/)
- `06-pi-review.md:7` — iMac review2 has correct/wrong/skip/trash/reclassify verdicts, history table, cl — **reason:** Framed as iMac-side context; spot-checked plausible in shared code (api.py:520 apply_verdict, api.py:2573-2583 client_id
- `07-thermal.md:9-18,69-92` — Observed temp/fan/clock distribution (avg 78.3C etc.) and the 2026-04-30 vcgencm — **reason:** Dated measurement records + hardware/vcgencmd behavior — historical, not current-state code claims
- `07-thermal.md:94-111` — 2026-05-11 thermal event narrative (213% CPU, 0xe0008, soak-test results) — **reason:** Historical record; the mechanisms it describes that persist (substream, thread pinning, prealloc, HLS demux) were verifi
- `08-deployment.md:47` — Pi's /home/vives/bird-classifier/.git exists from the original cp -a — **reason:** Pi runtime filesystem state, not verifiable from the code root
- `08-deployment.md:164` — The repo is public; pre-share scan found no secrets — **reason:** External GitHub state + one-time scan record; .gitignore coverage of the named files was verified separately
- `08-deployment.md:82` — hailortcli fw-control identify works — **reason:** Third-party CLI on Pi hardware; corroborated only indirectly by pi5_thermal_watch.py:95 docstring
- `09-the-unified-brain.md throughout (iMac column of comparison table, LaunchAgents, Coral, CoreML, reviews.db/birdnet_local.db locations+sizes, birds.vivessato.com serving)` — iMac-side current-state claims (~85 MB classifications.db, ~98 ms CoreML YOLO, C — **reason:** iMac machine/repo is outside the assigned code root; unverifiable from /Users/vives/bird-classifier-pi
- `09-the-unified-brain.md "What goes in the brain" + hardware rows` — Hardware/pricing claims (Pi 5 $80, Hailo kit $70, 27 W PSU, 12 W under load, the — **reason:** Hardware/third-party claims, explicitly out of scope per protocol
- `09-the-unified-brain.md migration levers, shadow-period, cutover mechanics, Stage 1-3, rollback, compare_classifications.py, pi-staging subdomain, source_brain tag` — The entire migration playbook (shadow writes, sqlite3_rsync cutover, DNS flip, d — **reason:** Chapter header declares "destination, not what runs today"; these are aspirational/planned, not current-state claims
- `09-the-unified-brain.md second-order lever #5` — Data movement uses the Tailscale IP (pi5.tailscale-net) — **reason:** Future-migration plan + third-party network infra; hostname is illustrative, not a code artifact
- `09-the-unified-brain.md "Cutting-edge research validation" + "References & further reading"` — External literature claims (sqlite3_rsync in SQLite 3.46, DuckDB sqlite_scanner  — **reason:** Third-party services/tools and a dated (2026-04-28) research-pass record — historical section, not code claims
- `09-the-unified-brain.md "When the work happens"` — David quotes and future migration-plan doc path ~/docs/bird-observatory-shared/< — **reason:** Dated quotes are historical record; the plan doc is explicitly future/not-yet-created
- `10-overlay-sync.md:72-74 ('What hasn't been solved' #2)` — RTL9210 page-cache-zombie root cause; enclosure-replacement decision pending — **reason:** Hardware/ops claims, not verifiable from this code root (session memory suggests the ASM2364 swap since landed, but that
- `10-overlay-sync.md:15-22, 45` — Thermal/CPU/soak measurements (213%, 84-86 C, 0xe0008, SSE ~30Hz, 15-min soak) — **reason:** Dated historical measurements, records not current-state claims
- `10-overlay-sync.md: avenues #4, #5, #7, #9` — Commit hashes 1503435 / 787810d / 0aeed01 and pre-1503435 gaussianAt/adaptiveAnc — **reason:** Git-history claims about removed code; historical by construction
- `10-overlay-sync.md: avenues #12, #13, #14` — Pi 5 has no HW H.264 decode / no HW encode; G3 Dome is H.264-only — **reason:** Hardware/third-party claims, out of scope per protocol
- `10-overlay-sync.md:84-277 (preserved 2026-05-10 prose details)` — '5-second clips', 'one decoder for both detection AND segmenting', hls.js-everyw — **reason:** Explicitly framed as preserved historical prose; covered by the new top note + the summarized drift entry rather than li
