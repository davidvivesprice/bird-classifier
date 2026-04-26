# Hailo-8L Playbook — Pi 5 AI Kit

**Audience:** future-you + future-Claude, working on the bird observatory.
**Hardware target:** Raspberry Pi 5 + Hailo-8L (13 TOPS, M.2 2242 via PCIe).
**Written:** 2026-04-25. Research done after we hit `HAILO_OUT_OF_PHYSICAL_DEVICES(74)` trying to run two Hailo models at once.

This is the "become ninjas with the whole ecosystem" reference. It's long. Skim the TL;DR, then jump to the section you need.

---

## 0 · TL;DR — the five things to know

1. **One `VDevice()` per process. Period.** A second `VDevice()` in the same process = `HAILO_OUT_OF_PHYSICAL_DEVICES(74)`. Instead: create one `VDevice` with a shared `group_id` and `scheduling_algorithm=ROUND_ROBIN`, then `vdevice.create_infer_model(hef)` once per model. HailoRT time-slices between them. This is the **officially supported** multi-model pattern.
2. **Hailo-8L has 1 vdevice slot. Hailo-8 has multiple.** We cannot have a Hailo YOLO detector AND a Hailo classifier running as independent VDevices — but we can have them sharing one VDevice via the scheduler (see #1).
3. **Compilation (DFC) runs only on x86_64 Ubuntu 22.04/24.04.** Not on Pi, not on Mac. You need a Linux box (cloud VM is fine: AWS g5/g6, Lambda, Paperspace) + a free Hailo developer account for the SDK downloads.
4. **`hailo-apps` is the canonical Python template now.** `Hailo-Application-Code-Examples` is *officially deprecated*. `hailo-rpi5-examples` is a thin wrapper over `hailo-apps`. TAPPAS is the C++/GStreamer heavyweight — only if you need full reference pipelines.
5. **For the observatory: use `InferModel` + `run_async` when you go multi-model.** `InferVStreams` (sync) is fine for a single model, but the scheduler can only interleave if you use the async API.

---

## 1 · Hardware — what's on the Pi

| Spec | Value |
|---|---|
| Accelerator | Hailo-8L (13 TOPS, INT8) |
| Connection | PCIe 1x Gen 3 via M.2 HAT |
| Physical VDevice slots | **1** (this is the important one) |
| Driver kernel module | `hailo_pci` |
| Userspace | `hailo-all` apt package = driver + firmware + HailoRT runtime + Python bindings + GStreamer plugins |
| Python module | `hailo_platform` (from `hailort_<ver>_arm64.deb`) |
| Pre-compiled HEFs | `/usr/share/hailo-models/` |
| Typical idle temp | ~55°C with fan |
| Typical load temp | 80-85°C (soft throttle at 80) — see `§8` |

**Versions on our Pi as of 2026-04-24:**
- HailoRT 4.19+ (via apt `hailo-all`)
- Python 3.13.5
- Kernel module `hailo_pci` auto-loaded at boot

---

## 2 · Runtime — HailoRT Python API

### 2.1 · Module map

All exported from `hailo_platform`. Source of truth:
`hailort/libhailort/bindings/python/platform/hailo_platform/pyhailort/pyhailort.py` (5252 lines — keep the link).

**Device & config**
- `VDevice(params=None)` — virtual device, context manager. One per process.
- `VDevice.create_params()` → `VDeviceParams`. Key attributes: `scheduling_algorithm`, `group_id`, `device_count`, `multi_process_service`.
- `HailoSchedulingAlgorithm` — IntEnum: `NONE` | `ROUND_ROBIN`. Use ROUND_ROBIN for multi-model.
- `HEF(path_or_bytes)` — loads a compiled model file.
- `ConfigureParams.create_from_hef(hef, interface)` — sync pipeline's pre-req. (Legacy.)
- `ConfiguredNetwork` / `ConfiguredNetworkGroup` — result of `vdevice.configure(hef, params)`.

**Sync path (simple, one-at-a-time)**
- `InferVStreams(cng, in_params, out_params)` → context manager → `.infer(dict) → dict`.
- `InputVStreamParams.make(...)` / `OutputVStreamParams.make(...)`.

**Async path (preferred, required for multi-model)**
- `VDevice.create_infer_model(hef_source, name="")` → `InferModel`.
- `InferModel` — setters: `batch_size`, `power_mode`, per-stream `set_format_type`, NMS params. `.configure()` → `ConfiguredInferModel`.
- `ConfiguredInferModel` — context manager. Key methods:
  - `create_bindings(input_buffers=None, output_buffers=None)` — per-frame holder
  - `wait_for_async_ready(timeout_ms, frames_count=1)`
  - `run_async(bindings, callback=None)` → `AsyncInferJob`
  - `shutdown()` — release
  - `set_scheduler_timeout(ms)`, `set_scheduler_threshold(n)`, `set_scheduler_priority(p)`
- `AsyncInferJob.wait(timeout_ms)` — only method.
- `AsyncInferCompletionInfo` — passed to callback; `.exception` is None on success.

**Exceptions** (all in `hailo_platform.pyhailort.pyhailort`):
`HailoRTException` (base), `HailoRTTimeout`, `HailoRTInvalidArgumentException`, `HailoRTNotFoundException`, `HailoRTInvalidHEFException`, `HailoRTHEFNotCompatibleWithDevice`, `HailoRTFirmwareControlFailedException`, `HailoRTDriverOperationFailedException`, `HailoRTStreamAborted`, `HailoRTNetworkGroupNotActivatedException`.

### 2.2 · Thread safety

- **One VDevice per process.** `VDevice` stores the creator PID; `create_infer_model()` and `configure()` from a different PID raise `HailoRTException`.
- `ConfiguredInferModel` is safe for concurrent `run_async` from multiple producer threads (HailoRT scheduler serializes internally).
- `Bindings` is single-use per in-flight frame. Don't reuse without waiting.
- Callbacks fire on an internal HailoRT worker thread → keep them short, don't block.
- Don't pass `ConfiguredNetworkGroup` or `InferVStreams` across thread/module boundaries.

### 2.3 · Canonical patterns

#### Pattern A — Single model, sync (our current `HailoDetector`)

```python
from hailo_platform import (VDevice, HEF, ConfigureParams, InferVStreams,
                            InputVStreamParams, OutputVStreamParams,
                            HailoStreamInterface, FormatType)

hef = HEF("/usr/share/hailo-models/yolov8s_h8l.hef")
with VDevice() as vdev:
    cfg = ConfigureParams.create_from_hef(hef, HailoStreamInterface.PCIe)
    cng = vdev.configure(hef, cfg)[0]
    in_p  = InputVStreamParams.make(cng, format_type=FormatType.UINT8)
    out_p = OutputVStreamParams.make(cng, format_type=FormatType.FLOAT32)
    with cng.activate(), InferVStreams(cng, in_p, out_p) as pipe:
        outputs = pipe.infer({input_name: np_batch})  # (N,H,W,C)
```

#### Pattern B — Two models, shared VDevice + scheduler (the fix for our error)

```python
from hailo_platform import VDevice, FormatType, HailoSchedulingAlgorithm

params = VDevice.create_params()
params.scheduling_algorithm = HailoSchedulingAlgorithm.ROUND_ROBIN
params.group_id = "SHARED"  # string, used as coalescing key
vdev = VDevice(params)

det = vdev.create_infer_model("yolov8s_h8l.hef")
det.set_batch_size(1)
cim_det = det.configure()
cim_det.__enter__()
cim_det.set_scheduler_priority(0)

cls = vdev.create_infer_model("efficientnet_lite0_birds_h8l.hef")
cls.set_batch_size(1)
cim_cls = cls.configure()
cim_cls.__enter__()
cim_cls.set_scheduler_priority(0)  # equal → round robin

# Optional tuning:
cim_det.set_scheduler_threshold(8)   # accumulate N frames before switch
cim_det.set_scheduler_timeout(ms=100)  # force switch after 100ms regardless
```

**Alternative (GStreamer):** two `hailonet` elements with the **same** `vdevice-group-id="SHARED"` — Hailo's own reference apps (e.g. `hailo-apps/.../face_recognition_pipeline.py`) use this pattern.

#### Pattern C — Async inference with callback

```python
from functools import partial
import numpy as np

def on_done(completion_info, bindings_list):
    if completion_info.exception:
        log.error("async infer failed: %s", completion_info.exception)
        return
    # KEEP SHORT. Don't block the HailoRT worker thread.
    out = bindings_list[0].output().get_buffer()
    result_queue.put_nowait(out.copy())  # copy! view is reclaimed after cb

def infer_frame(frame_np, cim, infer_model):
    out_buf = {n: np.empty(infer_model.output(n).shape, dtype=np.float32)
               for n in infer_model.output_names}
    b = cim.create_bindings(output_buffers=out_buf)
    b.input().set_buffer(np.ascontiguousarray(frame_np))
    cim.wait_for_async_ready(timeout_ms=10000)
    return cim.run_async([b], partial(on_done, bindings_list=[b]))

job = infer_frame(frame, cim, infer_model)
job.wait(timeout_ms=5000)  # optional — callback already fired when this returns
```

**Buffer lifetime gotcha:** `run_async` internally stores buffers in `self._buffer_guards` and pops them inside the callback wrapper. Never mutate the input ndarray before the callback runs.

#### Pattern D — Shutdown to avoid zombie VDevice

```python
try:
    # ... work ...
finally:
    if cim.last_infer_job:
        cim.last_infer_job.wait(10000)    # drain in-flight
    cim.shutdown()
    cim.__exit__(None, None, None)         # release C++ side
    # repeat for every ConfiguredInferModel
    vdev.release()                         # explicit — don't rely on __del__
```

Install a `signal.SIGTERM` handler that calls `vdev.release()` in long-running services. If the process is `SIGKILL`-ed mid-inference, the kernel driver holds the device "busy" for a few seconds; next launch either works or hits `HAILO_DEVICE_IN_USE(73)`.

### 2.4 · Sync vs async — decision guide

| Axis | `InferVStreams` (sync) | `InferModel.run_async` |
|---|---|---|
| Throughput | One in-flight batch; CPU idle during HW work | Overlaps CPU pre/post with HW → ~1.5-3× higher on Pi 5 |
| Latency | Slightly lower for a single frame | Same HW latency, better end-to-end (no CPU block) |
| Complexity | Low | Medium — bindings, callbacks, buffer lifetime |
| Multi-model | Works but blocks across models | **Required** for scheduler to interleave |
| Batch > 1 | Natural (ndarray) | Works (list of bindings) |
| Pick it when | Offline scripts; single-model eval | Live cameras, >1 model, or pipelining with CPU work |

**For the observatory:** keep sync while we have one YOLO. When we add the flagship classifier as a second HEF on the chip, port to async + shared VDevice.

---

## 3 · GStreamer plugin suite

When it makes sense (reference apps, demos, complex compositions), Hailo's GStreamer elements are how you build a real-time pipeline graph.

| Element | Purpose | Key properties |
|---|---|---|
| `hailonet` | Runs HEF inference in the pipeline. Wraps VDevice + ConfiguredInferModel internally. | `hef-path`, `batch-size`, `vdevice-group-id` (default `"SHARED"`), `scheduler-timeout-ms`, `scheduler-priority`, `multi-process-service`, `nms-score-threshold`, `nms-iou-threshold`, `output-format-type`, `force-writable` |
| `hailocropper` | Crops regions (detections) out of a frame into a sub-branch. Used to chain detector → classifier. | `so-path`, `function-name`, `use-letterbox`, `resize-method` (`bilinear`/`inter-area`), `internal-offset`, `no-scaling-bbox` |
| `hailoaggregator` | Sink for the cropper's two branches; merges classifier results back onto the main buffer. | `flatten-detections` |
| `hailotracker` | ByteTrack multi-object tracker; assigns stable track IDs. | `class-id`, `kalman-dist-thr`, `iou-thr`, `init-iou-thr`, `keep-new-frames`, `keep-tracked-frames`, `keep-lost-frames` |
| `hailooverlay` | Draws boxes/labels/masks from `HAILO_ROI` metadata. | `line-thickness`, `font-thickness` |
| `hailofilter` | Runs a C++ post-process `.so` on HEF output tensors. | `so-path`, `function-name`, `config-path` (JSON) |
| `hailotilecropper`/`hailotileaggregator` | SAHI-style grid crops for small-object inference. | `tiles-along-x-axis`, `overlap-*`, `iou-threshold`, `tiling-mode` |

**Shared-VDevice rule:** two `hailonet` elements with the *same* `vdevice-group-id` string share a VDevice under the hood. Different IDs = different VDevices = `OUT_OF_PHYSICAL_DEVICES` on Hailo-8L.

**Reference pipeline template:**
```
v4l2src ! ... ! hailonet hef-path=det.hef vdevice-group-id=SHARED !
  hailocropper so-path=crop.so internal-offset=true name=cropper
  cropper. ! queue ! hailonet hef-path=cls.hef vdevice-group-id=SHARED !
            hailofilter so-path=cls_post.so ! queue ! aggregator.
  cropper. ! queue ! aggregator.
  hailoaggregator name=aggregator ! hailooverlay ! ... ! videosink
```

---

## 4 · Compilation — DFC + Model Zoo

The offline toolchain. This is where a custom-trained ONNX becomes a `.hef` that the Pi can run.

### 4.1 · Platform constraint (hard)

DFC runs **only on x86_64 Ubuntu 22.04 or 24.04.** Python 3.10/3.11/3.12. Nvidia GPU recommended for optimize/finetune (Turing/Ampere, CUDA 12.5.1, driver 525+). 32 GB RAM minimum for realistic models.

**Not supported:** ARM, macOS, WSL1, CPU-only for optimize step (works but very slow). WSL2 on Windows works.

**Path of least resistance:** spin up a cloud VM (AWS g5/g6, Lambda, Paperspace) with Ubuntu 22.04 + Nvidia GPU + 32 GB RAM. Install:

```bash
pip install hailo_dataflow_compiler-3.33.0-cp310-cp310-linux_x86_64.whl
sudo dpkg -i hailort_4.23.0_amd64.deb
git clone -b v2.18 https://github.com/hailo-ai/hailo_model_zoo
cd hailo_model_zoo && pip install -e .
```

Or use the **Hailo AI Software Suite Docker image** which bundles DFC + HailoRT + Model Zoo + TAPPAS with versions pinned compatible. Much easier first time.

### 4.2 · Compilation flow (three stages)

```bash
# Stage 1: parse ONNX → HAR (Hailo Archive, intermediate)
hailomz parse <network_name> --ckpt model.onnx --yaml config.yaml --hw-arch hailo8l

# Stage 2: optimize (quantize to INT8, apply calibration)
hailomz optimize <network_name> --har parsed.har \
    --calib-path /path/to/1024+_calibration_images/ --hw-arch hailo8l

# Stage 3: compile HAR → HEF
hailomz compile <network_name> --har optimized.har --hw-arch hailo8l
```

**Calibration dataset:** ≥1024 unlabeled images, preprocessed to input shape (e.g. 224×224×3), directory of JPEGs OR a `.npy` array OR TFRecord. Should be representative of deployment distribution.

**ONNX input support:** opsets **8, 11-17**. Also accepts TF frozen `.pb`, TF2 SavedModel, `.tflite`.

### 4.3 · `hw-arch` matters

- `hailo8` — full 26 TOPS chip; HEFs for 8 **won't run** on 8L.
- `hailo8l` — our chip. HEFs for 8L **do run** on 8 (backward compatible).
- `hailo10h`, `hailo15h` — newer chips.

Always pass `--hw-arch hailo8l` for the Pi.

### 4.4 · Calibration pitfalls

- Too few images → quantization collapse (the yard-model 0/14 disaster was partly this).
- Wrong distribution → model works on calib data but fails in the wild.
- Some layers (LayerNorm, GELU, large FCs) quantize poorly → use `post_quantization_optimization(finetune, policy=enabled)` and/or mixed-precision.
- Compile for 8L specifically — 8L has fewer network-group resources, some models need splitting.

---

## 5 · Custom classifier path (Tier 2 flagship)

Our actual near-term goal: train a custom 16-class bird classifier (EfficientNet-Lite0) and deploy to Hailo-8L. Ordered flow:

### 5.1 · Huge finding from the research

**EfficientNet-Lite0 is a first-class Zoo citizen.** The Zoo ships:
- `cfg/networks/efficientnet_lite0.yaml` — declares `supported_hw_arch: [hailo8, hailo8l]`
- `cfg/alls/generic/efficientnet_lite0.alls` — ready-made compression/optimization recipe

Pre-cooked `alls` (verbatim):
```
norm_layer1 = normalization([127, 127, 127], [128, 128, 128])
quantization_param({conv*}, bias_mode=double_scale_initialization)
model_optimization_config(calibration, batch_size=32, calibset_size=64)
post_quantization_optimization(bias_correction, policy=enabled)
post_quantization_optimization(finetune, policy=disabled)
logits_layer1 = logits_layer(efficientnet_lite0/fc1, softmax, -1, cpu)
```

Note the softmax is offloaded to CPU — so the HEF output is raw logits and we apply softmax in Python.

### 5.2 · Concrete ordered flow

1. **Train on x86_64 GPU** (cloud VM or dev box). Use PyTorch or TF/Keras. Export ONNX with opset 11 when done. The Zoo doesn't ship an EfficientNet training Docker — bring your own trainer.
2. **Produce calibration set:** 1024+ yard-camera bird crops, unlabeled, 224×224×3 preprocessed. Should span lighting conditions, species distribution, distances.
3. **Fork the Zoo YAML:**
   - Copy `cfg/networks/efficientnet_lite0.yaml` → `cfg/networks/efficientnet_lite0_birds.yaml`
   - Change `paths.network_path` to your ONNX
   - Set `evaluation.classes: 16`
   - Verify `parser.nodes` (input/output tensor names) match your export — use Netron to check
4. **Parse + optimize + compile:**
   ```bash
   hailomz parse    efficientnet_lite0_birds --ckpt birds.onnx \
       --yaml cfg/networks/efficientnet_lite0_birds.yaml --hw-arch hailo8l
   hailomz optimize efficientnet_lite0_birds --har parsed.har \
       --calib-path /calib/ --hw-arch hailo8l
   hailomz compile  efficientnet_lite0_birds --har optimized.har --hw-arch hailo8l
   ```
5. **Drop HEF on Pi**, register in `pipeline/model_registry.py` as a new candidate, test via the Model Lab upload-test.
6. **Wire into pipeline** (the part that requires multi-model on one VDevice — see §2.3 Pattern B).

### 5.3 · Hairy/Downy specialist head

The training plan calls for a Hairy/Downy specialist head. For compilation purposes this is still a single ONNX with a multi-head architecture — Hailo compiles the whole graph. Nothing special in the DFC side; just make sure both heads' outputs are named and referenced in the YAML's `parser.nodes`.

### 5.4 · Quantization-aware training (QAT)

Hailo supports QAT via the `post_quantization_optimization(finetune, policy=enabled)` directive in the `alls` file. This does a short fine-tune pass during optimize using the calibration set. For rare-species sensitive models, consider QAT. Setting it up fully is a Phase 3+ task for us.

---

## 6 · Model Zoo re-training — what it does and doesn't

The Zoo is primarily a **compilation + evaluation** library, not a training one. Training Dockers exist only for a subset:
- YOLOv3/4/5/8/X, DAMO-YOLO, NanoDet (detectors)
- CenterPose, MSPN (pose)
- FCN, YOLACT, YOLOv8-seg (segmentation)
- ArcFace, ViT (classifiers — but not general-purpose)

**No EfficientNet training Docker.** For our flagship, we train elsewhere and hand the ONNX to the Zoo's compile path.

Pretrained imagenet weights for `efficientnet_lite0` are on Hailo's S3 (URL in the YAML). Useful to verify compile path before you have your own weights, not useful for birds (wrong classes).

---

## 7 · Error catalog — the most valuable table

Codes from `hailort/libhailort/include/hailo/hailort.h`.

| Code | Meaning | Typical cause | Diagnosis | Fix |
|------|---------|---------------|-----------|-----|
| **74** `HAILO_OUT_OF_PHYSICAL_DEVICES` | Not enough physical devices | 2nd `VDevice()` in same process | `lsmod \| grep hailo`, `hailortcli scan`, `fuser -v /dev/hailo0` | Single shared `VDevice(group_id="SHARED", scheduling_algorithm=ROUND_ROBIN)`; call `vdevice.release()` on shutdown. |
| **73** `HAILO_DEVICE_IN_USE` | Device already in use | Prev process died without release; another service holds it | `sudo lsof /dev/hailo0`; `systemctl status hailort.service` | Stop other consumer; `sudo rmmod hailo_pci && sudo modprobe hailo_pci`. |
| **4** `HAILO_TIMEOUT` | Operation timed out | Bindings not submitted in time; HEF/FW mismatch; thermal throttle | Increase `timeout_ms`; `hailortcli fw-control identify`; `hailortcli sensors` | Match HEF ↔ HailoRT ↔ FW versions; raise `timeout_ms` to 10000 for large models; check thermal. |
| **2** `HAILO_INVALID_ARGUMENT` | Bad arg | Wrong input shape/dtype; non-contiguous ndarray; unknown vstream name | Print `infer_model.input().shape, .format`; check `buffer.flags.c_contiguous` | `np.ascontiguousarray(...)`; cast to `uint8`; pass full vstream name. |
| **8** `HAILO_INTERNAL_FAILURE` | Unexpected internal failure | Malformed postprocess config JSON; corrupted HEF; driver/runtime mismatch | `hailortcli parse-hef foo.hef`; `hailortcli fw-control identify` | Fix config JSON; re-download HEF matching HailoRT; `sudo apt install --reinstall hailo-all`. |
| **3** `HAILO_OUT_OF_HOST_MEMORY` | Host OOM | Too many big models/bindings; fragmentation | `free -h`; `cat /proc/meminfo` | Reduce `batch_size`; `set_scheduler_threshold(1)`; on Pi 5 4GB don't run 2 YOLOv8s + classifier with batch>1. |
| **65** `HAILO_NOT_AVAILABLE` | Component unavailable | Calling into released VDevice; `activate()` while scheduler is on | Check object lifetimes | Re-create device; never `activate()` when `scheduling_algorithm != NONE`. |
| **64** `HAILO_DRIVER_NOT_INSTALLED` | Driver not loaded | Kernel module missing or wrong kernel | `lsmod \| grep hailo`; `dmesg \| grep hailo`; `dkms status` | `sudo apt install --reinstall hailo-all`; `sudo dkms autoinstall`; reboot. |
| **36** `HAILO_DRIVER_OPERATION_FAILED` | Driver ioctl failed | PCIe link drop; USB-PCIe flake; power budget | `dmesg -T \| grep hailo` | Reseat HAT; use official Pi 5 5V5A PSU; `dtoverlay=pciex1-compat-pi5` in config.txt. |
| **87** `HAILO_DRIVER_TIMEOUT` | Driver operation timeout | Stuck DMA, usually after unclean shutdown | `dmesg` | `sudo hailortcli fw-control reset`; if persistent, reload module. |
| **93** `HAILO_HEF_NOT_COMPATIBLE_WITH_DEVICE` | HEF wrong for device | Ran Hailo-8 HEF on 8L | `hailortcli parse-hef foo.hef` (shows arch) | Recompile for `hailo8l`, or download `-h8l` variant. |
| **26/91/94** `HEF_*` | Invalid/corrupt HEF | Truncated download; shared-weights misuse | md5 the file; `hailortcli parse-hef` | Re-download; read full bytes if loading from memory. |

---

## 8 · Diagnostic tools

### 8.1 · `hailortcli` — your main CLI

Source: `hailort/hailortcli/`.

| Subcommand | What |
|---|---|
| `fw-control identify` | FW + driver + HailoRT versions + device ID. **Run this first when debugging.** |
| `fw-control reset` | Soft reset the chip |
| `scan` | List physical devices on PCIe/USB |
| `run <hef>` | Single-network benchmark with real data |
| `run2 <configs>` | Multi-network scenario (simulates scheduler), uses `group_id` |
| `benchmark` | Throughput/latency/power sweep |
| `parse-hef <hef>` | Dumps arch, network groups, I/O shapes, FPS estimate. **Use this to answer "is this HEF for my device?"** |
| `sensors` | Chip temperature, voltage, current draw |
| `measure-power`, `measure-nnc-performance` | Deeper perf counters |
| `monitor` | Real-time scheduler + FPS to stdout (pair with `HAILO_MONITOR=1`) |

### 8.2 · Environment variables

From `hailort/common/env_vars.hpp`:

| Var | Purpose |
|---|---|
| `HAILORT_LOGGER_PATH` | Where to write the rotating `hailort.log` file |
| `HAILORT_CONSOLE_LOGGER_LEVEL` | `trace` / `debug` / `info` / `warning` / `error` / `critical` / `off` |
| `HAILO_MONITOR=1` | Enables scheduler monitor dump |
| `HAILO_MONITOR_TIME_INTERVAL` | ms between dumps |
| `HAILO_TRACE=scheduler` | Enables profiler trace (.hrtt) |
| `HAILO_TRACE_PATH` | Override trace output path |
| `HAILO_PERFETTO_TRACE=1` | Use Perfetto format instead of proprietary |

### 8.3 · Where Hailo logs land on Pi 5

- **`hailort.log`** — written to the process's **current working directory** by default. So if `bird-pipeline.service` has `WorkingDirectory=%h/bird-classifier`, the log is at `~/bird-classifier/hailort.log`. Override with `HAILORT_LOGGER_PATH=/var/log/hailort/`.
- **Kernel driver** — `dmesg` or `journalctl -k`, look for `hailo_pci`.
- **Multi-process service (if used)** — `journalctl -u hailort.service`.

### 8.4 · Profiler

`HAILO_TRACE=scheduler` + run your app → `/tmp/hailort_profiler_<pid>.hrtt` file. Open with Hailo's GUI profiler (in the full AI Software Suite, not the apt package). For quick checks, the `hailortcli monitor` subcommand is often enough.

---

## 9 · Decision matrix — implementation paths

Named paths for the multi-model problem, ranked cheapest → most work.

| Path | What you write | Expected FPS impact | Pi caveats | Confidence |
|---|---|---|---|---|
| **P4 · Classifier on CPU (current)** | Nothing. | Zero change. AIY ONNX at 7.4 ms/frame. | None. | Measured — this is our live state. |
| **P1 · Single VDevice, shared `group_id`, one process** | One `VDevice(ROUND_ROBIN, group_id="SHARED")` + `vdevice.create_infer_model()` per model. ~50 lines. | Time-sliced. With MobileNetV2 at 1738 FPS alone, even 20% slice = 350 FPS, far above need. Detector stays dominant cost. | Keep `hailort.service` OFF. One process only. Always `vdevice.release()` on shutdown. | **High** — Hailo-official pattern, used by shipping reference apps on 8L. Un-measured in our pipeline. |
| **P2 · DeGirum PySDK compound model** | Few lines: two `dg.load_model()` + `CroppingAndClassifyingCompoundModel`. | Same scheduler underneath as P1. Small Python overhead. | DeGirum advises `hailort.service` off on Pi 5 — aligned with P1. | **High** for correctness, medium for perf overhead vs raw HailoRT (not measured). |
| **P5 · Detector on CPU, classifier on Hailo** | YOLOv8n via ONNXRuntime/OpenCV on Pi 5 CPU; classifier alone on Hailo. | YOLOv8n on Pi 5 CPU: ~5-10 FPS (community est, not in sources). Classifier essentially free on chip. | Frees NPU for Tier-2 experiments. Gives up the big detector-FPS win. | **Low** — CPU YOLOv8n FPS on Pi 5 not in sources. |
| **P3 · `hailort.service` + two processes** | Enable systemd service; `multi_process_service=True` on both. | Adds IPC overhead per inference. Hailo-apps auto-**disables** this on 8/8L. DeGirum explicitly advises against on Pi 5. | Source of HAILO_RPC_FAILED in v4.18 thread. | Most work + worst expected perf. **Avoid** unless you genuinely need two processes for other reasons. |

**Recommendation for us:** **P1 when we add the flagship classifier.** P4 stays for now (AIY ONNX on CPU is fast enough).

---

## 10 · Ecosystem map — which repo to template from

| Repo | Status | When to use |
|---|---|---|
| **`hailo-ai/hailort`** (raw HailoRT) | Current | Small headless inference services (e.g. our `HailoDetector`). No GStreamer deps, minimal footprint. |
| **`hailo-ai/hailo-apps`** | Current, **canonical Python template** | Pipelines, GStreamer apps, multi-model references. Contains `HailoInfer` (shared-VDevice scheduler wrapper). |
| **`hailo-ai/hailo-rpi5-examples`** | Current, thin | Pi 5 setup scripts + demo launchers; depends on `hailo-apps`. |
| **`hailo-ai/Hailo-Application-Code-Examples`** | **DEPRECATED** — README says so explicitly | Read the old `hailo_inference.py` for the clean async pattern. Don't build on it. |
| **`hailo-ai/tappas`** | C++/GStreamer heavyweight | Full reference pipelines with trackers, re-ID, DeepStream-like composition. Overkill for us. |
| **`hailo-ai/hailo_model_zoo`** (tag `v2.18`) | Current | Compilation, evaluation, `hailomz` CLI. **Use the `v2.18` tag** — master is Hailo-10/15 only (v5.x). |
| **`DeGirum/hailo_examples`** | Third-party, excellent | `CroppingAndClassifyingCompoundModel` for quick multi-model setup. Alternative to rolling our own. |

**Our case (Pi 5 + Hailo-8L + Python + existing raw HailoRT):**
- Keep `HailoDetector` on raw HailoRT.
- When adding the flagship classifier, copy `HailoInfer` (50 lines, MIT) from `hailo-apps/.../hailo_inference.py` — don't import the whole package.
- If we later want a GStreamer composition UI, add `hailo-apps` then.

---

## 11 · Bookmarks

### Public (no login)
- https://github.com/hailo-ai/hailort
- https://github.com/hailo-ai/hailo-apps
- https://github.com/hailo-ai/hailo-rpi5-examples
- https://github.com/hailo-ai/hailo_model_zoo/tree/v2.18 (**v2.18 tag**, not master)
- https://github.com/hailo-ai/tappas
- https://github.com/DeGirum/hailo_examples
- https://community.hailo.ai — forum
- Model Zoo HAILO8L tables: `https://github.com/hailo-ai/hailo_model_zoo/tree/v2.18/docs/public_models/HAILO8L/`

### Login-walled (free account at https://hailo.ai/developer-zone/)
- Developer Zone hub
- SW downloads — DFC `.whl`, HailoRT `.deb`, AI Software Suite Docker
- DFC User Guide PDF (current v3.33; third-party mirrors have v3.27/3.30/3.31)

### In this repo
- `pipeline/hailo_detector.py` — our current `HailoDetector` (YOLOv8 on Hailo)
- `pipeline/hailo_classifier.py` — Hailo classifier wrapper (used by Model Lab)
- `pipeline/model_registry.py` — candidate registry with Pi-aware `exclude_hailo` kwarg
- `models/aiy_birds_v1.onnx` — current classifier (CPU)

---

## 12 · Empirical unknowns (things to test, not trust)

Neither research pass nailed these down. Flag-and-test:

1. **[MEASURED 2026-04-25]** Actual detector FPS when classifier is co-scheduled on Hailo-8L. Bench: `tools/bench_hailo_multimodel.py` (YOLOv8s_h8l + ResNet50_h8l, 200 iters, dummy zeros, ROUND_ROBIN scheduler).
   - **Isolated:** YOLOv8s p50=16.97 ms (58.9 FPS); ResNet50 p50=20.97 ms (47.7 FPS). Detector matches the Zoo's 58.67 FPS table.
   - **Co-scheduled (interleaved DET→CLS):** YOLOv8s mean=22.0 ms (45.5 FPS, **−23%**); ResNet50 mean=22.6 ms (44.2 FPS, **−7%**). Aggregate 22.4 iters/s = 44.6 ms per (det+cls) pair → ~6 ms scheduler overhead per iter.
   - **What this means:** Hailo-8L can comfortably host both YOLOv8s AND a ~25 ms classifier with the live pipeline's 5 FPS sub-stream target (we're 9× over budget). Multi-model Path 1 (playbook §9) is unblocked at the throughput level. Bursty bird events that need rapid classification will tax the chip more than the steady-state bench (still well within margin).
2. **Scheduler `threshold` / `timeout` values for bursty workloads** (a bird appears → classifier bursts for ~10 frames → idle). hailonet exposes them; no guidance on values.
3. **Pi 5 CPU cost of GStreamer pipeline vs raw HailoRT Python for same workload.** `hailo-apps` uses GStreamer + C++ post-process .so files. Our code is raw HailoRT Python. Moving to GStreamer might free CPU or burn it — unknown.
4. **8L network-group capacity limit.** We know it's "lower than 8" but no source quantifies it. Practical: a detector + 1 classifier is fine; haven't seen 3+ models on 8L documented.
5. **CPU-side YOLOv8n FPS on Pi 5.** Would decide whether Path 5 (detector-on-CPU) is even viable. Not in sources.
6. **Whether DFC supports 4-bit weights for Hailo-8L.** Zoo's generic `alls` doesn't expose it. Unknown.
7. **Our Pi's thermal ceiling when running ring buffer + detector + future classifier.** Currently 83-84°C with ring + detector + AIY-CPU, fan maxed. Adding a second Hailo model *might* push it over.

---

## 13 · What this means for the bird observatory

Given the research, our actual plan:

**Near-term (this sprint):**
- Keep AIY on CPU (P4). Fast enough (7.4 ms), no NPU contention, 83-84°C thermal ceiling comfortable.
- Model Lab on Pi can upload-test Hailo classifiers in isolation (already works — gets the whole chip for one inference).
- Live pipeline switch is restart-based (already built) — only CPU candidates are marked available in the pipeline-view registry.

**Medium-term (Tier 2 Phase 1-5):**
- Train EfficientNet-Lite0 for 16-class birds on x86 GPU.
- Compile via DFC + Model Zoo for `hailo8l`.
- Evaluate standalone on Model Lab upload-test.
- Shadow deploy as a CPU candidate first (we have headroom).

**Medium-term (Tier 2 Phase 6-8):**
- Flip to P1 pattern: single VDevice, scheduler, detector + flagship classifier on Hailo together.
- Measure real FPS with both loaded.
- Tune scheduler thresholds to smooth bursty classification loads.

**Stretch:**
- GStreamer migration if we want a richer real-time viz pipeline.

---

## 14 · Sources

Already synthesized into this doc from two research passes:

- DeGirum `hailo_examples` — https://github.com/DeGirum/hailo_examples
- Hailo community forum threads (linked inline in §7 and §2)
- `hailo-ai/hailort` — master
- `hailo-ai/hailo-apps` — main
- `hailo-ai/hailo-rpi5-examples` — main
- `hailo-ai/hailo_model_zoo` — **tag v2.18**
- `hailo-ai/tappas` — v5.3.0
- Ridgerun "Convert ONNX Model to Hailo-8L" — https://www.ridgerun.ai/post/convert-onnx-model-to-hailo8l
- Hailo Model Zoo HAILO8L classification FPS table — https://github.com/hailo-ai/hailo_model_zoo/tree/v2.18/docs/public_models/HAILO8L/
- DFC User Guide — login-walled; third-party mirrors have v3.27/3.30/3.31

Canonical reference files on disk (tmp copies from research):
- `/tmp/pyhailort.py` — Python binding source, 5252 lines
- `/tmp/hailort.h` — all error codes
- `/tmp/hailo_inference.py` — 216-line async shared-VDevice template
- `/tmp/gst_helper.py` + `/tmp/gst_helper.md` — GStreamer pipeline builder

---

**Update cadence:** append findings when we hit new errors or measure empirical numbers. Version this doc alongside the repo — if we ever regenerate it, diff against this version.
