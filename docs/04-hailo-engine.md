# 04 · Hailo Engine — multi-model on one VDevice

The Hailo-8L has exactly **one** physical VDevice slot. Two `VDevice()` instances in one process raise `HAILO_OUT_OF_PHYSICAL_DEVICES(74)`. Two processes both holding a VDevice — same error.

The Pi observatory wants both a YOLOv8 detector AND a classifier on Hailo (now: nothing, AIY runs on CPU; soon: the Tier 2 flagship). The way to do that on Hailo-8L is the official multi-model pattern: one shared `VDevice` with `scheduling_algorithm=ROUND_ROBIN` and `group_id="SHARED"`, plus an `InferModel` per HEF, all driven through `run_async`. HailoRT time-slices.

This file is the Pi-side architecture summary; the deep reference (every API call, error code, scheduler tuning, compilation flow) lives at `working/specs/2026-04-25-hailo-playbook.md`.

## The engine

`pipeline/hailo_engine.py` is a process-singleton owner of the VDevice. Public surface:

```python
HailoEngine.get()                  # lazy singleton
HailoEngine.acquire_model(hef_path) # → HailoModel (cached per path)
HailoModel.infer(inputs: dict)      # → dict[output_name, ndarray]
HailoEngine.shutdown()              # release the VDevice
```

Inside, `HailoEngine.__init__` does:

```python
params = hp.VDevice.create_params()
params.scheduling_algorithm = hp.HailoSchedulingAlgorithm.ROUND_ROBIN
params.group_id = "SHARED"
self._vdevice = hp.VDevice(params)
```

`acquire_model(path)` calls `vdevice.create_infer_model(path)` and wraps it in `HailoModel`. The wrapper lazy-configures on first `infer()` (so unit tests can construct without exercising HailoRT runtime), and runs every infer through `cim.run_async([bindings])` + `job.wait(timeout_ms=10000)` — sync semantics, scheduler-friendly internals.

## What `HailoModel.infer` does per call

1. Allocate FLOAT32 output buffers from the model's `output_names` + spec shapes.
2. Build bindings via `cim.create_bindings(output_buffers=...)`.
3. For each input, `bindings.input(name).set_buffer(np.ascontiguousarray(arr))`. If the buffer isn't writable (e.g. PIL→numpy view), force a copy.
4. `cim.wait_for_async_ready(timeout_ms=10000)`.
5. `job = cim.run_async([bindings]); job.wait(timeout_ms=10000)`.
6. Copy output buffers out (callback-side memory may be reclaimed) and return.

Output dtype is FLOAT32 by default. The engine calls `infer_model.output(name).set_format_type(FormatType.FLOAT32)` BEFORE `configure()` for every output, so callers always read FLOAT32 regardless of the HEF's native quantized output format. This was a bench-driven fix: ResNet50_h8l natively emits UINT8 quantized softmax (1000 bytes for 1000 classes); without `set_format_type(FLOAT32)` the engine's pre-allocated FLOAT32 buffer (4000 bytes) would mismatch.

## What HEFs we use

| Name | Path | Used by | is_classifier |
|---|---|---|---|
| `yolov8s_h8l.hef` | `/usr/share/hailo-models/yolov8s_h8l.hef` | `HailoDetector` (live pipeline) | n/a (it's a detector) |
| `resnet_v1_50_h8l.hef` | `/usr/share/hailo-models/resnet_v1_50_h8l.hef` | Lab upload-test as ImageNet baseline; live-classifier candidate | True |
| `yolov6n_h8l.hef` | `/usr/share/hailo-models/yolov6n_h8l.hef` | Lab upload-test only | False |

Available HEFs are surfaced via `pipeline/model_registry.py:build_default_registry()`. Each candidate has a `description` + `notes` (one-phrase metadata) + `info` (the multi-paragraph deep-dive shown in the dashboard's per-model lightbox — see `05-dashboard.md`).

`is_classifier=False` blocks the candidate from being selected as the live pipeline classifier. Detector-type HEFs would emit COCO labels ("bird" / "cat") in the live overlay if you tried to use them as classifiers; the dashboard's `/api/models/switch` returns 400 with a helpful message.

## Pre-compiled HEF input quirks

Pre-compiled HEFs from the Hailo Model Zoo (the ones in `/usr/share/hailo-models/`) bake the normalization layer into the graph. Pass raw UINT8 0-255 pixels — don't pre-normalize to FLOAT32 [0, 1]. `HailoClassifier` was previously doing `arr.astype(np.float32) / 255.0` on the assumption that the legacy `InferVStreams` API would auto-quantize; with the new InferModel path it doesn't. Drop the / 255 step and pass UINT8 — the HEF's own norm layer handles it.

(Tier 2 models we compile ourselves will follow the same convention: bake the norm layer in via the Model-Zoo `alls`, present a UINT8 input.)

## YOLOv8 NMS output format

The InferModel path emits NMS-baked YOLO output as a flat FLOAT32 ndarray, not the legacy list-of-arrays. Format: densely-packed per-class blocks `[count_c, det0_5fl, det1_5fl, ..., count_c+1, ...]` for 80 COCO classes. `_parse_yolo_flat_output` in `pipeline/hailo_detector.py` walks the variable-length blocks; bounds-checked for ragged buffers.

Each detection's 5 floats are `[y1, x1, y2, x2, conf]` in normalized [0, 1] coords relative to the model's input size. The parser un-letterboxes them back into frame coords.

## Co-scheduled performance (measured 2026-04-25)

Bench script: `tools/bench_hailo_multimodel.py` (run with `bird-pipeline.service` stopped, dummy zero inputs, 200 iters per model).

| Model | Isolated p50 / FPS | Co-scheduled mean / FPS | Δ |
|---|---|---|---|
| YOLOv8s | 16.97 ms / 58.9 | 22.0 ms / 45.5 | −23 % |
| ResNet-50 | 20.97 ms / 47.7 | 22.6 ms / 44.2 | −7 % |

Combined: 22.4 iters/s = 44.6 ms per (det+cls) pair → ~6 ms scheduler context-switch overhead per pair.

Pipeline target throughput is 5 FPS on the substream. Even after the co-schedule penalty we have ~9× headroom on the detector. The takeaway: multi-model on Hailo-8L works for our workload; the chip is not the bottleneck for either single-model or co-scheduled inference.

## Lifecycle

- Created lazily on first `HailoEngine.get()` call.
- Per-model `HailoModel` objects also lazy: configured on first `infer()`.
- `HailoEngine.shutdown()` releases all configured infer-models then the VDevice. Called by `bird_pipeline_v3.py` on shutdown signal.
- If the process is `SIGKILL`-ed mid-inference, the kernel driver holds the device "busy" for a few seconds. Use `systemctl --user restart` (graceful) — not `pkill -9`.

## Testing without HW

`tests/pipeline/test_hailo_engine.py`, `test_hailo_detector_engine.py`, `test_hailo_classifier_engine.py` use a fake `hailo_platform` module injected via pytest's `monkeypatch.setitem(sys.modules, ...)`. They run on the iMac without Hailo HW and verify singleton-ness, model caching, format-type configuration, and (for the detector) the flat-output parser shape.

## Future Tier 2 deployment

When the EfficientNet-Lite0 flagship is compiled and dropped at `~/bird-classifier/models/<name>.hef`, registering it as a new `CandidateModel` in `build_default_registry` is enough to make it live-switchable. The engine handles cohabitation with YOLOv8s automatically. See `historical/specs/2026-04-23-tier2-training-plan-v1.md` and `historical/specs/2026-04-23-tier2-data-audit.md` for the training-side plan.
