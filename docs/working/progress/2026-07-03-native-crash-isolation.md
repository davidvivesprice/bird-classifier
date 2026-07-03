# Native-crash isolation — the "loses the bird" root cause, fixed structurally

**Problem.** The pipeline SEGV-crashed for weeks (~80 crashes/3 days at worst,
bursty under load). Every crash restarted the whole process and wiped every
track — the primary reliability cause of the live view "losing the bird."

## What we proved (in order)

1. **The crash is not one bug — it is two independent native crashers:**
   - **libav**: `av_vlog <- av_log <- av_read_frame` on the PyAV decode
     thread. Identical backtrace across dozens of cores. Reproducer: 1080p
     demo file replay ≈ 2 SEGVs / 140 s.
   - **libhailort 4.23**: after decode was isolated (2026-07-03), the parent
     *kept* crashing under demo load; fresh cores put the fault in
     `/lib/libhailort.so.4.23.0` with **no libav frames present** — proving
     decode isolation worked AND a second crasher existed in Hailo inference.
2. **The libav crash is NOT fixable via `av.logging`.** Four callback states
   tested under the reproducer — PyAV's no-op default (`nolog_callback`),
   PyAV's Python `log_callback`, libav's C `av_log_default_callback`, and
   NULL installed via ctypes — **all crash at the same rate**. The fault is
   inside libav's own read path, upstream of the callback dispatch.
   `pipeline/av_log_guard.py` is retained only to silence log spam and to
   document the dead end. Do not re-attempt logging-level fixes.
3. **Root-cause fixes inside the libraries are out of reach** (libav internal
   bug; hailort firmware/driver pairing can't be safely upgraded unattended).

## The fix: one supervised cage for both crashers

`pipeline/frame_capture_proc.py` — the child process decodes (PyAV) **and**
detects (HailoDetector) each frame, shipping `(frame, pts, wall_ms,
detections)` through a shared-memory ring. The parent process keeps only
pure-Python + CPU state: tracker, AIY classifier (onnx CPU), SSE, health.

- Child SEGV → supervisor respawns it (~2–4 s incl. Hailo VDevice init) →
  the tracker **coasts** through the gap (`hit_counter_max=150` ≈ 5 s was
  built for exactly this) → **tracks and locks survive**.
- `PrecomputedDetector` shim honors the BirdDetector interface, so
  `process_thread` is unchanged.
- `PIPELINE_DECODE_INPROC=1` reverts to the fully in-process wiring.
- Regression test `tests/pipeline/test_frame_capture_proc.py` SIGSEGVs the
  real child and asserts frames resume + the parent survives.

## Verified

- Suite: 458 passed / 0 failed on the Pi (isolation tests included).
- 10-minute bird-dense demo soak (both crashers under sustained load):
  parent PID unchanged, 0 parent SEGVs, detections flowing through the ring
  at full rate. (See session notes 2026-07-03; soak CSV in /tmp on the Pi.)

## Residual risks / follow-ups

- **HLS segmenter** (`pipeline/hls_recorder.py`) still runs PyAV
  (`av_read_frame` on the 1080p main stream) **in the parent** on the LIVE
  path (demo disables it). If live parent SEGVs persist, cage the segmenter
  next — same ring/supervisor pattern.
- While the child owns the Hailo VDevice, Model-Lab **Hailo** classifiers
  cannot load in the parent (CPU models unaffected; `aiy_onnx` is current).
- `yolo_ms_avg` in health now measures the shim (~0 ms); true Hailo time is
  `frame.det_ms` (shipped from the child, not yet surfaced in health).
- Known cosmetic: teardown-time GC SEGV in the *offline replay tool* after
  its output is written (harmless; exit path only).
