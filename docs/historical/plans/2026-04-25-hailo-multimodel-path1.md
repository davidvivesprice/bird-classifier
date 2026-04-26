> **HISTORICAL** — This document describes a design, plan, or system state that has been superseded. Kept for decision-trail context.

# Multi-model Hailo (Path 1) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `HailoDetector` and `HailoClassifier` coexist on the Hailo-8L's single VDevice slot using HailoRT's scheduler, so the live pipeline can run a Hailo classifier alongside the YOLOv8s detector.

**Architecture:** Introduce a process-singleton `HailoEngine` that owns one shared `VDevice(group_id="SHARED", scheduling_algorithm=ROUND_ROBIN)` and lazy-creates `InferModel`s on it. Refactor `HailoDetector` and `HailoClassifier` to acquire models from the engine instead of constructing private VDevices. Drop the `exclude_hailo=True` guard once both can run together. Measure detector + classifier co-scheduled FPS — that's playbook §12 empirical unknown #1.

**Tech Stack:** HailoRT 4.19+ Python bindings (`hailo_platform` — `VDevice`, `InferModel`, `ConfiguredInferModel`, `AsyncInferJob`), Python 3.13 on Pi 5, existing pipeline contracts unchanged.

**Reference:** `docs/superpowers/specs/2026-04-25-hailo-playbook.md` §0 (one-VDevice rule), §2.3 (Pattern B + C), §9 (Path 1), §12 (unknowns).

**Pi-only files:** `hailo_*.py`, `model_registry.py`. No PI_MODE-gating needed; iMac doesn't run these.

---

### Task 1: Add `HailoEngine` singleton + sync `HailoModel` wrapper

**Files:**
- Create: `pipeline/hailo_engine.py`
- Test: `tests/pipeline/test_hailo_engine.py`

The engine owns the process's only `VDevice`. `HailoModel` wraps an `InferModel` and exposes a sync `infer(inputs: dict) -> dict` that internally drives `run_async + AsyncInferJob.wait()` so the scheduler can interleave. Output shape is a dict keyed by output vstream name — matches what `InferVStreams.infer()` returns today, so detector/classifier callsites barely change.

- [ ] **Step 1.1: Write the failing test for engine singleton + sync infer**

```python
# tests/pipeline/test_hailo_engine.py
"""Engine tests use mocks of hailo_platform to keep the suite portable
(no Hailo HW required on iMac dev box). Live HW exercised via the bench
script in Task 5 and the live Pi restart in Task 7.
"""
import sys
import threading
import types
from unittest.mock import MagicMock

import numpy as np
import pytest


def _install_fake_hailo_platform(monkeypatch):
    """Inject a stand-in `hailo_platform` module so the engine can import
    without real Hailo hardware. Returns the stand-in for assertion."""
    fake = types.ModuleType("hailo_platform")

    class _SchedAlgo:
        ROUND_ROBIN = 1
        NONE = 0
    fake.HailoSchedulingAlgorithm = _SchedAlgo

    class _FmtType:
        UINT8 = "uint8"
        FLOAT32 = "float32"
    fake.FormatType = _FmtType

    class _StreamIface:
        PCIe = 0
    fake.HailoStreamInterface = _StreamIface

    class _VDeviceParams:
        def __init__(self):
            self.scheduling_algorithm = None
            self.group_id = None
            self.device_count = None
            self.multi_process_service = False

    fake._created_vdevices = []

    class _VDevice:
        def __init__(self, params=None):
            self.params = params
            self.released = False
            self._models = []
            fake._created_vdevices.append(self)

        @staticmethod
        def create_params():
            return _VDeviceParams()

        def create_infer_model(self, hef_path, name=""):
            m = MagicMock(name=f"InferModel({hef_path})")
            m.hef_path = hef_path
            self._models.append(m)
            return m

        def release(self):
            self.released = True

    fake.VDevice = _VDevice

    monkeypatch.setitem(sys.modules, "hailo_platform", fake)
    # If pipeline.hailo_engine was imported in a prior test, force re-import
    if "pipeline.hailo_engine" in sys.modules:
        del sys.modules["pipeline.hailo_engine"]
    return fake


def test_engine_creates_one_shared_vdevice(monkeypatch):
    fake = _install_fake_hailo_platform(monkeypatch)
    from pipeline import hailo_engine
    hailo_engine.HailoEngine._reset_for_testing()  # idempotent reset

    eng = hailo_engine.HailoEngine.get()
    assert eng is hailo_engine.HailoEngine.get(), "Engine must be a singleton"
    assert len(fake._created_vdevices) == 1
    vd = fake._created_vdevices[0]
    assert vd.params.scheduling_algorithm == fake.HailoSchedulingAlgorithm.ROUND_ROBIN
    assert vd.params.group_id == "SHARED"


def test_engine_acquire_model_reuses_per_path(monkeypatch):
    fake = _install_fake_hailo_platform(monkeypatch)
    from pipeline import hailo_engine
    hailo_engine.HailoEngine._reset_for_testing()

    eng = hailo_engine.HailoEngine.get()
    m1 = eng.acquire_model("/path/a.hef")
    m2 = eng.acquire_model("/path/a.hef")
    m3 = eng.acquire_model("/path/b.hef")
    assert m1 is m2, "Same HEF path must return same handle (caching)"
    assert m3 is not m1
    # VDevice should have been asked to create exactly two InferModels
    vd = fake._created_vdevices[0]
    assert len(vd._models) == 2


def test_engine_shutdown_releases_vdevice(monkeypatch):
    fake = _install_fake_hailo_platform(monkeypatch)
    from pipeline import hailo_engine
    hailo_engine.HailoEngine._reset_for_testing()

    eng = hailo_engine.HailoEngine.get()
    eng.acquire_model("/x.hef")
    eng.shutdown()
    assert fake._created_vdevices[0].released is True
```

- [ ] **Step 1.2: Run test to verify it fails**

```
cd /Users/vives/bird-classifier && ./venv-coral/bin/python3 -m pytest tests/pipeline/test_hailo_engine.py -v
```
Expected: FAIL with `ModuleNotFoundError: No module named 'pipeline.hailo_engine'`

- [ ] **Step 1.3: Implement `HailoEngine` + `HailoModel`**

```python
# pipeline/hailo_engine.py
"""Process-singleton owner of the Pi's Hailo VDevice.

Hailo-8L has exactly ONE physical VDevice slot. To run multiple HEFs in the
same process — e.g. our YOLOv8 detector AND a future EfficientNet-Lite0
classifier — we share a single VDevice with HailoRT's ROUND_ROBIN scheduler
and drive each model through ``InferModel.run_async``. Per playbook §0, §9.

Public surface:
- ``HailoEngine.get()`` — lazy singleton
- ``HailoEngine.acquire_model(hef_path)`` → ``HailoModel`` (cached per path)
- ``HailoModel.infer(inputs: dict[str, np.ndarray]) -> dict[str, np.ndarray]``
- ``HailoEngine.shutdown()`` — release VDevice (idempotent)

Sync ``infer`` is implemented over ``run_async`` + ``AsyncInferJob.wait`` so
the scheduler can interleave with other HailoModels on the same engine.
"""
from __future__ import annotations

import logging
import threading
from typing import Dict, Optional

import numpy as np

log = logging.getLogger(__name__)


class HailoModel:
    """Sync wrapper over an InferModel that uses the scheduler-friendly
    async path internally. ``infer({input_name: ndarray}) -> {output_name: ndarray}``.
    """

    def __init__(self, infer_model, hef_path: str):
        import hailo_platform as hp
        self._hp = hp
        self._infer_model = infer_model
        self.hef_path = hef_path
        infer_model.set_batch_size(1)
        # Lazy-configure on first infer so unit tests can construct without HW.
        self._cim = None  # ConfiguredInferModel
        self._cim_ctx_entered = False
        self._lock = threading.Lock()

    def _ensure_configured(self):
        if self._cim is not None:
            return
        cim = self._infer_model.configure()
        # ConfiguredInferModel is a context manager; enter it manually so we
        # control lifecycle.
        cim.__enter__()
        self._cim = cim
        self._cim_ctx_entered = True

    def infer(self, inputs: Dict[str, np.ndarray]) -> Dict[str, np.ndarray]:
        with self._lock:
            self._ensure_configured()
            cim = self._cim
            # Build output buffers from the InferModel's output spec.
            out_buffers: Dict[str, np.ndarray] = {}
            for name in self._infer_model.output_names:
                spec = self._infer_model.output(name)
                out_buffers[name] = np.empty(spec.shape, dtype=np.float32)
            bindings = cim.create_bindings(output_buffers=out_buffers)
            # Single input case (typical for YOLO/classifier HEFs).
            for name, arr in inputs.items():
                bindings.input(name).set_buffer(np.ascontiguousarray(arr))
            cim.wait_for_async_ready(timeout_ms=10000)
            job = cim.run_async([bindings])
            job.wait(timeout_ms=10000)
            # Copy out (callback-side buffers may be reclaimed)
            return {n: out_buffers[n].copy() for n in out_buffers}

    @property
    def input_names(self):
        return list(self._infer_model.input_names)

    @property
    def output_names(self):
        return list(self._infer_model.output_names)

    def input_shape(self, name: Optional[str] = None):
        if name is None:
            name = self.input_names[0]
        return self._infer_model.input(name).shape

    def output_shape(self, name: Optional[str] = None):
        if name is None:
            name = self.output_names[0]
        return self._infer_model.output(name).shape

    def close(self):
        with self._lock:
            cim = self._cim
            if cim is not None:
                try:
                    if hasattr(cim, "last_infer_job") and cim.last_infer_job is not None:
                        cim.last_infer_job.wait(5000)
                except Exception:
                    pass
                try:
                    cim.shutdown()
                except Exception:
                    pass
                if self._cim_ctx_entered:
                    try:
                        cim.__exit__(None, None, None)
                    except Exception:
                        pass
                self._cim = None


class HailoEngine:
    """Process-singleton VDevice owner. Use ``HailoEngine.get()``."""

    _instance_lock = threading.Lock()
    _instance: Optional["HailoEngine"] = None

    def __init__(self):
        import hailo_platform as hp
        self._hp = hp
        params = hp.VDevice.create_params()
        params.scheduling_algorithm = hp.HailoSchedulingAlgorithm.ROUND_ROBIN
        params.group_id = "SHARED"
        self._vdevice = hp.VDevice(params)
        self._models: Dict[str, HailoModel] = {}
        self._models_lock = threading.Lock()
        log.info("HailoEngine started: shared VDevice (group_id=SHARED, ROUND_ROBIN)")

    @classmethod
    def get(cls) -> "HailoEngine":
        with cls._instance_lock:
            if cls._instance is None:
                cls._instance = cls()
            return cls._instance

    def acquire_model(self, hef_path: str) -> HailoModel:
        with self._models_lock:
            if hef_path in self._models:
                return self._models[hef_path]
            infer_model = self._vdevice.create_infer_model(hef_path)
            wrapped = HailoModel(infer_model, hef_path)
            self._models[hef_path] = wrapped
            log.info("HailoEngine.acquire_model: %s", hef_path)
            return wrapped

    def shutdown(self):
        with self._models_lock:
            for m in self._models.values():
                m.close()
            self._models.clear()
        try:
            self._vdevice.release()
        except Exception:
            pass
        log.info("HailoEngine shutdown")

    @classmethod
    def _reset_for_testing(cls):
        """Drop the singleton so the next get() rebuilds. Tests only."""
        with cls._instance_lock:
            if cls._instance is not None:
                try:
                    cls._instance.shutdown()
                except Exception:
                    pass
                cls._instance = None
```

- [ ] **Step 1.4: Run test, verify it passes**

```
./venv-coral/bin/python3 -m pytest tests/pipeline/test_hailo_engine.py -v
```
Expected: 3 passed.

- [ ] **Step 1.5: Commit**

```
git add pipeline/hailo_engine.py tests/pipeline/test_hailo_engine.py
git commit -m "feat(hailo): HailoEngine singleton + HailoModel sync wrapper"
```

---

### Task 2: Refactor `HailoDetector` to use the engine

**Files:**
- Modify: `pipeline/hailo_detector.py:31-78` (constructor) and `:82-132` (detect)
- Test: `tests/pipeline/test_hailo_detector_engine.py` (new — engine integration)

The legacy `HailoDetector` builds its own `VDevice` + uses sync `InferVStreams`. After this task it acquires its `InferModel` from the engine and calls `HailoModel.infer({input_name: x})`. The output dict is shaped the same way `InferVStreams.infer()` returned, so the existing `_parse_yolo_list_output` path still works.

- [ ] **Step 2.1: Write the failing engine-integration test**

```python
# tests/pipeline/test_hailo_detector_engine.py
"""HailoDetector must use HailoEngine.acquire_model — no private VDevice."""
import sys
import types
from unittest.mock import MagicMock
import numpy as np
import pytest


def _install_fake_hailo_for_detector(monkeypatch):
    """Same shape as test_hailo_engine but with HEF + InferModel adequate
    for the detector to construct."""
    fake = types.ModuleType("hailo_platform")

    class _SchedAlgo:
        ROUND_ROBIN = 1
        NONE = 0
    fake.HailoSchedulingAlgorithm = _SchedAlgo

    class _FmtType:
        UINT8 = "uint8"
        FLOAT32 = "float32"
    fake.FormatType = _FmtType

    class _StreamIface:
        PCIe = 0
    fake.HailoStreamInterface = _StreamIface

    class _VDeviceParams:
        def __init__(self):
            self.scheduling_algorithm = None
            self.group_id = None

    fake._created_vdevices = []

    class _InferModelOutputSpec:
        def __init__(self, shape):
            self.shape = shape

    class _InferModel:
        def __init__(self, hef_path):
            self.hef_path = hef_path
            self.input_names = ["input"]
            self.output_names = ["output"]
            self._batch = None
            self._ins = {"input": _InferModelOutputSpec((640, 640, 3))}
            self._outs = {"output": _InferModelOutputSpec((80, 5, 100))}

        def set_batch_size(self, n):
            self._batch = n

        def input(self, name):
            return self._ins[name]

        def output(self, name):
            return self._outs[name]

        def configure(self):
            cim = MagicMock(name=f"CIM({self.hef_path})")
            cim.last_infer_job = None
            def _ctx_enter(*a, **k): return cim
            def _ctx_exit(*a, **k): return None
            cim.__enter__ = _ctx_enter
            cim.__exit__ = _ctx_exit
            def _create_bindings(output_buffers=None, input_buffers=None):
                b = MagicMock()
                # input(...).set_buffer captured but no-op
                return b
            cim.create_bindings = _create_bindings
            cim.wait_for_async_ready = MagicMock()
            class _Job:
                def wait(self, timeout_ms=0): return None
            cim.run_async = MagicMock(return_value=_Job())
            cim.shutdown = MagicMock()
            return cim

    class _VDevice:
        def __init__(self, params=None):
            self.params = params
            self.released = False
            self._models = []
            fake._created_vdevices.append(self)

        @staticmethod
        def create_params():
            return _VDeviceParams()

        def create_infer_model(self, hef_path, name=""):
            m = _InferModel(hef_path)
            self._models.append(m)
            return m

        def release(self):
            self.released = True

    fake.VDevice = _VDevice

    # legacy bits — left in for any path that still uses sync; mocked but unused
    fake.HEF = MagicMock()
    fake.ConfigureParams = MagicMock()
    fake.InputVStreamParams = MagicMock()
    fake.OutputVStreamParams = MagicMock()
    fake.InferVStreams = MagicMock()

    monkeypatch.setitem(sys.modules, "hailo_platform", fake)
    for mod in ["pipeline.hailo_engine", "pipeline.hailo_detector"]:
        if mod in sys.modules:
            del sys.modules[mod]
    return fake


def test_detector_uses_shared_engine(monkeypatch):
    fake = _install_fake_hailo_for_detector(monkeypatch)
    from pipeline.hailo_engine import HailoEngine
    HailoEngine._reset_for_testing()

    from pipeline.hailo_detector import HailoDetector
    det1 = HailoDetector("/det.hef", confidence=0.3)
    det2 = HailoDetector("/cls.hef", confidence=0.3)
    # Two detectors, ONE VDevice
    assert len(fake._created_vdevices) == 1
    vd = fake._created_vdevices[0]
    assert vd.params.scheduling_algorithm == fake.HailoSchedulingAlgorithm.ROUND_ROBIN
    # Two InferModels (one per HEF)
    assert len(vd._models) == 2
```

- [ ] **Step 2.2: Run test to verify it fails**

```
./venv-coral/bin/python3 -m pytest tests/pipeline/test_hailo_detector_engine.py -v
```
Expected: FAIL — current detector creates its own VDevice → `len(fake._created_vdevices) == 2` not 1.

- [ ] **Step 2.3: Refactor `HailoDetector.__init__` to use engine**

Replace lines 43-78 with:

```python
        from pipeline.hailo_engine import HailoEngine

        # Resolve I/O shape from the HEF (we still need the shape for letterbox).
        # The engine wraps construction; we pull metadata from the HailoModel.
        self._model = HailoEngine.get().acquire_model(hef_path)
        self._input_name = self._model.input_names[0]
        self._output_name = self._model.output_names[0]
        self._input_shape = self._model.input_shape()    # (h, w, c)
        self._output_shape = self._model.output_shape()
        # Stats
        self.stats = {
            "detect_calls": 0,
            "total_detections": 0,
            "bird_detections": 0,
            "last_ms": 0.0,
        }
        log.info("HailoDetector ready: in=%s out=%s", self._input_shape, self._output_shape)
```

Drop the `import hailo_platform as hp; self._hp = hp; self._hef = hp.HEF(...)` lines and the `vdevice.configure(...)`-based setup. The `_letterbox` / `_parse_yolo_list_output` helpers stay.

Replace `detect()` body's inference block (lines 105-112) with:

```python
        output = self._model.infer({self._input_name: x})
```

Drop `with self._network_group.activate(...)` and `with hp.InferVStreams(...)`. Drop the `import time` inside detect (move to module top).

Replace `close()` (lines 134-139) with:

```python
    def close(self):
        # Engine owns the VDevice; nothing to release here. Per-model cleanup
        # happens via HailoEngine.shutdown() at process exit.
        pass
```

- [ ] **Step 2.4: Run new + existing tests**

```
./venv-coral/bin/python3 -m pytest tests/pipeline/test_hailo_engine.py tests/pipeline/test_hailo_detector_engine.py tests/pipeline/test_detector.py -v
```
Expected: all pass. (test_detector.py covers the BirdDetector/ONNX path — should still pass since we don't touch it.)

- [ ] **Step 2.5: Commit**

```
git add pipeline/hailo_detector.py tests/pipeline/test_hailo_detector_engine.py
git commit -m "refactor(hailo): HailoDetector uses HailoEngine — no private VDevice"
```

---

### Task 3: Refactor `HailoClassifier` to use the engine

**Files:**
- Modify: `pipeline/hailo_classifier.py:99-131` (`_setup`) and `:135-166` (`classify`)
- Test: `tests/pipeline/test_hailo_classifier_engine.py` (new)

Same pattern as Task 2.

- [ ] **Step 3.1: Write the failing engine-integration test for classifier**

```python
# tests/pipeline/test_hailo_classifier_engine.py
"""HailoClassifier must use HailoEngine — no private VDevice. Confirms
that a HailoDetector and HailoClassifier in the same process create
exactly ONE VDevice (the cohabitation we need on Hailo-8L)."""
import sys
import pytest
import numpy as np

from tests.pipeline.test_hailo_detector_engine import _install_fake_hailo_for_detector  # noqa: E402


def test_classifier_uses_shared_engine(monkeypatch):
    fake = _install_fake_hailo_for_detector(monkeypatch)
    from pipeline.hailo_engine import HailoEngine
    HailoEngine._reset_for_testing()

    from pipeline.hailo_detector import HailoDetector
    from pipeline.hailo_classifier import HailoClassifier

    det = HailoDetector("/yolov8s.hef")
    cls = HailoClassifier("/resnet50.hef")

    assert len(fake._created_vdevices) == 1, (
        "Detector + Classifier must share ONE VDevice on Hailo-8L"
    )
    vd = fake._created_vdevices[0]
    assert len(vd._models) == 2
```

- [ ] **Step 3.2: Run test to verify it fails**

```
./venv-coral/bin/python3 -m pytest tests/pipeline/test_hailo_classifier_engine.py -v
```
Expected: FAIL with `len(fake._created_vdevices) == 2` (each constructs its own).

- [ ] **Step 3.3: Refactor `HailoClassifier._setup` and `classify`**

Replace `_setup` body (lines 99-131) with:

```python
    def _setup(self):
        from pipeline.hailo_engine import HailoEngine
        self._model = HailoEngine.get().acquire_model(self.hef_path)
        # PIL resize order is (w, h); InferModel input shape is typically (h, w, c).
        in_shape = self._model.input_shape()
        if len(in_shape) == 3:
            h, w, _ = in_shape
            self._input_shape = (w, h)
        else:
            self._input_shape = (224, 224)
        log.info("HailoClassifier[%s] ready. Input shape: %s",
                 self.kind, self._input_shape)
```

Drop `self._hp`, `self._hef`, `self._vdevice`, `self._network_group*`, `self._{input,output}_vstreams_params` — none are needed once the engine owns them.

Replace `classify()` inference block (lines 152-162) with:

```python
        input_name = self._model.input_names[0]
        output = self._model.infer({input_name: x})
```

Drop `with self._network_group.activate(...)` and `with hp.InferVStreams(...)`.

Replace `close()` (lines 214-219) with:

```python
    def close(self):
        # Engine owns the VDevice; nothing to release here.
        pass
```

- [ ] **Step 3.4: Run all Hailo tests**

```
./venv-coral/bin/python3 -m pytest tests/pipeline/test_hailo_engine.py tests/pipeline/test_hailo_detector_engine.py tests/pipeline/test_hailo_classifier_engine.py -v
```
Expected: all pass.

- [ ] **Step 3.5: Commit**

```
git add pipeline/hailo_classifier.py tests/pipeline/test_hailo_classifier_engine.py
git commit -m "refactor(hailo): HailoClassifier uses HailoEngine"
```

---

### Task 4: Drop `exclude_hailo` plumbing now that cohabitation works

**Files:**
- Modify: `pipeline/model_registry.py:155-220` (remove the `exclude_hailo` parameter and the `_hailo_conflict_note` plumbing)
- Modify: `bird_pipeline_v3.py` (search for the `exclude_hailo=True` call site and drop the kwarg)
- Modify: `dashboard/api.py` if it calls `build_default_registry(exclude_hailo=...)` — this lives in the `_get_pipeline_view_registry()` and `_get_model_registry()` functions per the handoff §2.C
- Test: extend `tests/pipeline/test_pipeline_classifier.py` if it asserts on `exclude_hailo=True` behavior; remove that assertion

- [ ] **Step 4.1: Locate all callers**

```
grep -rn "exclude_hailo" /Users/vives/bird-classifier --include="*.py"
```
Expected callers: `bird_pipeline_v3.py` (the pipeline-side build), `dashboard/api.py` (`_get_pipeline_view_registry`).

- [ ] **Step 4.2: Drop the kwarg from all callers + remove from `build_default_registry`**

In `model_registry.py:155`, change signature to:

```python
def build_default_registry(models_dir: str) -> ModelRegistry:
```

Remove the `_hailo_conflict_note` and any `available=... and not exclude_hailo` clauses (line 197, 219). Hailo candidates are now available as long as their HEF exists.

In `bird_pipeline_v3.py`: change `build_default_registry(models_dir, exclude_hailo=True)` to `build_default_registry(models_dir)`.

In `dashboard/api.py`: same simplification — `_get_pipeline_view_registry()` and `_get_model_registry()` should call `build_default_registry(models_dir)` (single function now, no Lab-vs-pipeline distinction).

- [ ] **Step 4.3: Run all classifier/registry tests**

```
./venv-coral/bin/python3 -m pytest tests/pipeline/test_pipeline_classifier.py tests/test_api_endpoints.py -v
```
Expected: all pass; any test that hard-coded `exclude_hailo=True` failure semantics needs to be deleted/updated to assert the new behavior (Hailo candidate available).

- [ ] **Step 4.4: Commit**

```
git add pipeline/model_registry.py bird_pipeline_v3.py dashboard/api.py tests/
git commit -m "refactor(model-registry): drop exclude_hailo — engine handles cohabitation"
```

---

### Task 5: Add `tools/bench_hailo_multimodel.py` benchmark tool

**Files:**
- Create: `tools/bench_hailo_multimodel.py`

Pi-only script. Acquires YOLOv8s + ResNet50 from the engine, runs N=200 frames each through both, reports per-model FPS, p50/p99 latency, combined load wall-clock duration. Single-process, real Hailo HW required.

- [ ] **Step 5.1: Write the script**

```python
#!/usr/bin/env python3
"""Benchmark detector + classifier co-scheduled on Hailo-8L.

Resolves playbook §12 empirical unknown #1: actual detector FPS when a
classifier is also loaded on the same VDevice via the ROUND_ROBIN scheduler.

Run on the Pi: `~/bird-classifier/venv/bin/python3 tools/bench_hailo_multimodel.py`
Note: the live bird-pipeline holds a HailoModel for YOLOv8s. For a clean
reading, EITHER stop the pipeline service OR run from the same process —
we run a separate process here, so the bench will see HAILO_DEVICE_IN_USE
unless the live pipeline is paused. Recommended workflow: stop bird-pipeline,
run bench, restart bird-pipeline.
"""
import argparse
import statistics
import sys
import time
from pathlib import Path

import numpy as np


def _bench_one(model, name: str, dummy_input, n: int) -> dict:
    times_ms = []
    for _ in range(n):
        t0 = time.monotonic()
        model.infer({model.input_names[0]: dummy_input})
        times_ms.append((time.monotonic() - t0) * 1000)
    return {
        "name": name,
        "n": n,
        "p50_ms": statistics.median(times_ms),
        "p99_ms": sorted(times_ms)[max(0, int(0.99 * n) - 1)],
        "mean_ms": statistics.fmean(times_ms),
        "fps": 1000.0 / statistics.fmean(times_ms),
    }


def _bench_interleaved(det_model, cls_model, det_in, cls_in, n: int) -> dict:
    """Drive both models alternating, measure aggregate throughput."""
    t0 = time.monotonic()
    for _ in range(n):
        det_model.infer({det_model.input_names[0]: det_in})
        cls_model.infer({cls_model.input_names[0]: cls_in})
    elapsed_s = time.monotonic() - t0
    return {
        "iterations": n,
        "elapsed_s": elapsed_s,
        "det_fps": n / elapsed_s,
        "cls_fps": n / elapsed_s,
        "combined_per_iter_ms": elapsed_s * 1000.0 / n,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--det", default="/usr/share/hailo-models/yolov8s_h8l.hef")
    ap.add_argument("--cls", default="/usr/share/hailo-models/resnet_v1_50_h8l.hef")
    ap.add_argument("--n", type=int, default=200)
    args = ap.parse_args()

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from pipeline.hailo_engine import HailoEngine

    eng = HailoEngine.get()
    det = eng.acquire_model(args.det)
    cls = eng.acquire_model(args.cls)

    det_in = np.zeros(det.input_shape(), dtype=np.uint8)
    cls_in = np.zeros(cls.input_shape(), dtype=np.float32)

    print("Single-model warmup + measure:")
    for label, m, x in (("DET", det, det_in), ("CLS", cls, cls_in)):
        # Warmup
        for _ in range(20):
            m.infer({m.input_names[0]: x})
        r = _bench_one(m, label, x, args.n)
        print(f"  {label}: p50={r['p50_ms']:.2f} ms  p99={r['p99_ms']:.2f} ms  "
              f"mean={r['mean_ms']:.2f} ms  → {r['fps']:.1f} FPS")

    print("\nInterleaved (DET→CLS→DET→CLS …):")
    r = _bench_interleaved(det, cls, det_in, cls_in, args.n)
    print(f"  {args.n} iters in {r['elapsed_s']:.3f} s")
    print(f"  combined per-iter: {r['combined_per_iter_ms']:.2f} ms "
          f"→ {r['det_fps']:.1f} det FPS  +  {r['cls_fps']:.1f} cls FPS")

    eng.shutdown()


if __name__ == "__main__":
    main()
```

- [ ] **Step 5.2: Mark executable + commit**

```
chmod +x tools/bench_hailo_multimodel.py
git add tools/bench_hailo_multimodel.py
git commit -m "feat(bench): hailo-multimodel co-scheduled FPS benchmark"
```

---

### Task 6: Run the benchmark on Pi, capture numbers, append to playbook

**Files:**
- Modify: `docs/superpowers/specs/2026-04-25-hailo-playbook.md` §12 (replace empirical unknown #1 with the measured numbers + a dated `[measured 2026-04-25]` note)

Run:

```
ssh vives@pi5.local "systemctl --user stop bird-pipeline && sleep 3 && \
    ~/bird-classifier/venv/bin/python3 ~/bird-classifier/tools/bench_hailo_multimodel.py 2>&1 | tee ~/logs/bench_hailo_multimodel_$(date +%Y%m%d-%H%M).txt && \
    systemctl --user start bird-pipeline"
```

- [ ] **Step 6.1: Run benchmark and capture output**

(Live HW step — output goes into the playbook update.)

- [ ] **Step 6.2: Update playbook §12 with measured numbers**

Edit `docs/superpowers/specs/2026-04-25-hailo-playbook.md`. Find the line that begins "Actual detector FPS when classifier is co-scheduled on Hailo-8L." and replace with the actual number, prefixed with the date. Same for any other unknowns we resolved.

- [ ] **Step 6.3: Commit**

```
git add docs/superpowers/specs/2026-04-25-hailo-playbook.md
git commit -m "docs(playbook): empirical unknown #1 measured 2026-04-25"
```

---

### Task 7: Live verification on Pi — restart pipeline + switch to a Hailo classifier via Lab

**Files:** none — this is operational verification.

- [ ] **Step 7.1: rsync all changes to Pi**

```
rsync -av /Users/vives/bird-classifier/pipeline/hailo_*.py \
          /Users/vives/bird-classifier/pipeline/model_registry.py \
          /Users/vives/bird-classifier/bird_pipeline_v3.py \
          /Users/vives/bird-classifier/dashboard/api.py \
          /Users/vives/bird-classifier/tools/bench_hailo_multimodel.py \
    vives@pi5.local:/home/vives/bird-classifier/
```
(adjust paths so each file lands at the right place)

- [ ] **Step 7.2: Restart pipeline**

```
ssh vives@pi5.local "systemctl --user restart bird-pipeline && sleep 8 && \
    curl -sS http://localhost:8100/api/pipeline/health | python3 -m json.tool | head -30"
```
Expected: pipeline.feeder block populated, detector running, no `HAILO_OUT_OF_PHYSICAL_DEVICES` errors in the log.

- [ ] **Step 7.3: Switch model via Lab UI / API**

```
ssh vives@pi5.local "curl -sS -X POST http://localhost:8099/api/models/switch \
    -H 'Content-Type: application/json' \
    -d '{\"name\": \"resnet50_hailo\"}' | python3 -m json.tool"
```
Expected: `{"ok": true, "current": "resnet50_hailo"}` (or a similar success response — exact shape depends on Pi vs iMac path; this hits the `_pi_update_env_classifier` flow which restarts the pipeline).

- [ ] **Step 7.4: Wait for restart, confirm both models active simultaneously**

```
ssh vives@pi5.local "sleep 12 && curl -sS http://localhost:8100/api/pipeline/health | python3 -m json.tool"
ssh vives@pi5.local "tail -50 ~/logs/bird-pipeline.log | grep -i hailo"
```
Expected: pipeline up, both detector AND classifier logged as ready, capture stats incrementing, no Hailo errors.

- [ ] **Step 7.5: Update memory + handoff**

Append a "What I shipped" section to `docs/superpowers/progress/2026-04-25-pi5-handoff.md`. Update `~/.claude/projects/-Users-vives/memory/project_pi5_overnight_build.md` with the empirical FPS numbers.

---

## Risks / Watch-outs

1. **Engine + scheduler quirks.** The playbook's gotcha §2.3: callbacks fire on a HailoRT worker thread — keep them short. We don't use callbacks (sync wrapper just `wait()`s), so this is fine, but worth re-checking if we add concurrent inference.
2. **Process exit cleanliness.** Per playbook §2.3 Pattern D, `vdevice.release()` should be called before process exit. We call it in `HailoEngine.shutdown()` but the bird-pipeline service uses `Restart=always`; if it gets `SIGKILL`-ed mid-inference, the kernel driver holds the device "busy" briefly. Fine for our usage.
3. **Test cohabitation between Hailo tests.** Each test calls `HailoEngine._reset_for_testing()` to drop the singleton. Skipping this between tests would carry state across.
4. **Bench requires the live pipeline stopped.** Documented in the script docstring. If we forget, we get `HAILO_DEVICE_IN_USE(73)` and the bench bails fast — annoying but not destructive.
5. **iMac coordination.** `model_registry.py` is Pi-only per handoff §2.B but `dashboard/api.py` is shared. The kwarg drop for `_get_pipeline_view_registry` should not break iMac — iMac never sets `exclude_hailo=True` (it has no Hailo). But ping iMac-Claude in comms before pushing the dashboard/api.py edit.

## Self-review (run before considering this plan ready)

- [ ] All seven tasks have concrete file paths + line numbers + complete code.
- [ ] No "TBD" / "implement later" / "fill in details" anywhere.
- [ ] Type/method names are consistent across tasks (`HailoModel`, `HailoEngine`, `acquire_model`, `infer`, `shutdown`).
- [ ] Tests are written before implementations in every task.
- [ ] Bench script is the only Pi-HW step before Task 7.
- [ ] Coordination: Task 4 touches `dashboard/api.py` (shared file) — flagged in Risks #5 to ping iMac-Claude.
