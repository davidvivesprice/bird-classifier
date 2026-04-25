"""HailoDetector must use HailoEngine.acquire_model — no private VDevice.

Two HailoDetectors (or HailoDetector + HailoClassifier) in the same
process should yield exactly ONE VDevice — that's the cohabitation
the Hailo-8L single-slot constraint forces.
"""
from __future__ import annotations

import sys
import types
from unittest.mock import MagicMock

import numpy as np
import pytest


def _install_fake_hailo_for_detector(monkeypatch):
    """Same shape as test_hailo_engine but with InferModel surface
    adequate for the detector to construct."""
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

    class _Spec:
        def __init__(self, shape):
            self.shape = shape

    class _InferModel:
        def __init__(self, hef_path):
            self.hef_path = hef_path
            self.input_names = ["input"]
            self.output_names = ["output"]
            self._batch = None
            self._ins = {"input": _Spec((640, 640, 3))}
            self._outs = {"output": _Spec((80, 5, 100))}

        def set_batch_size(self, n):
            self._batch = n

        def input(self, name):
            return self._ins[name]

        def output(self, name):
            return self._outs[name]

        def configure(self):
            cim = MagicMock(name=f"CIM({self.hef_path})")
            cim.last_infer_job = None

            def _ctx_enter(*a, **k):
                return cim

            def _ctx_exit(*a, **k):
                return None

            cim.__enter__ = _ctx_enter
            cim.__exit__ = _ctx_exit

            def _create_bindings(output_buffers=None, input_buffers=None):
                b = MagicMock()
                inp = MagicMock()
                inp.set_buffer = MagicMock()
                b.input = MagicMock(return_value=inp)
                return b

            cim.create_bindings = _create_bindings
            cim.wait_for_async_ready = MagicMock()

            class _Job:
                def wait(self, timeout_ms=0):
                    return None

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

    # Legacy bits — kept as MagicMocks in case anything still imports them
    # during the transition. Will be unused after Task 2 lands.
    fake.HEF = MagicMock()
    fake.ConfigureParams = MagicMock()
    fake.InputVStreamParams = MagicMock()
    fake.OutputVStreamParams = MagicMock()
    fake.InferVStreams = MagicMock()

    monkeypatch.setitem(sys.modules, "hailo_platform", fake)
    for mod in [
        "pipeline.hailo_engine",
        "pipeline.hailo_detector",
        "pipeline.hailo_classifier",
    ]:
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
    # Two detectors → ONE VDevice (the Hailo-8L cohabitation rule).
    assert len(fake._created_vdevices) == 1, (
        f"Two detectors must share one VDevice; got "
        f"{len(fake._created_vdevices)}"
    )
    vd = fake._created_vdevices[0]
    assert vd.params.scheduling_algorithm == fake.HailoSchedulingAlgorithm.ROUND_ROBIN
    # Two InferModels (one per HEF)
    assert len(vd._models) == 2


def test_detector_input_output_metadata_via_engine(monkeypatch):
    fake = _install_fake_hailo_for_detector(monkeypatch)
    from pipeline.hailo_engine import HailoEngine
    HailoEngine._reset_for_testing()

    from pipeline.hailo_detector import HailoDetector
    det = HailoDetector("/det.hef")
    # The detector pulls metadata from HailoModel
    assert det._input_shape == (640, 640, 3)
    assert det._output_shape == (80, 5, 100)
    assert det._input_name == "input"
    assert det._output_name == "output"


def test_parse_yolo_flat_output_decodes_dense_blocks():
    """The InferModel flat NMS output is densely packed per class:
    [count_c0, det0_5fl, ..., countN_cK_5fl, count_c1, ...] for 80 classes.
    """
    from pipeline.hailo_detector import _parse_yolo_flat_output

    # Construct a 3-class scenario: c0 has 1 det, c1 has 0, c2 has 2 dets,
    # remaining 77 classes empty. Total floats = 1 + 5 + 1 + 1 + 10 + 77*1
    # = 95.
    #   c0=1 det, normalized box [0.10, 0.20, 0.30, 0.40], conf=0.8
    #   c1=0 dets
    #   c2=2 dets:  [0.50, 0.50, 0.60, 0.60, 0.95]
    #               [0.05, 0.05, 0.15, 0.15, 0.40]  ← below threshold 0.5
    arr = [1.0, 0.10, 0.20, 0.30, 0.40, 0.8,
           0.0,
           2.0, 0.50, 0.50, 0.60, 0.60, 0.95,
                0.05, 0.05, 0.15, 0.15, 0.40]
    arr.extend([0.0] * 77)  # remaining classes empty
    arr_np = np.asarray(arr, dtype=np.float32)

    # Frame 640×360, model 640×640. Letterbox: scale = min(640/640, 640/360)
    # = 1.0 (limited by width). pad_y = (640 - 360*1) / 2 = 140.
    out = _parse_yolo_flat_output(
        arr_np,
        input_hw=(640, 640),
        frame_hw=(360, 640),
        confidence_threshold=0.5,
        accept_classes={0, 2},
    )
    # c0 detection passes (conf 0.8) — 1 hit.
    # c2 first passes (0.95), c2 second drops (0.40 < 0.5) — 1 hit.
    assert len(out) == 2
    classes = sorted(d["class"] for d in out)
    assert classes == [0, 2]
    confs = sorted(d["confidence"] for d in out)
    assert confs == [pytest.approx(0.8), pytest.approx(0.95)]


def test_parse_yolo_flat_output_filters_unaccepted_classes():
    from pipeline.hailo_detector import _parse_yolo_flat_output

    # c0 has 1 detection (high conf); we don't accept c0.
    arr = [1.0, 0.10, 0.20, 0.30, 0.40, 0.95]
    arr.extend([0.0] * 79)
    arr_np = np.asarray(arr, dtype=np.float32)

    out = _parse_yolo_flat_output(
        arr_np,
        input_hw=(640, 640),
        frame_hw=(640, 640),
        confidence_threshold=0.3,
        accept_classes={14},  # bird only
    )
    assert out == []


def test_parse_yolo_flat_output_truncated_buffer_safe():
    """Ragged / truncated buffers must not crash."""
    from pipeline.hailo_detector import _parse_yolo_flat_output

    # Says "5 detections" but only 1.5 detections worth of data remain.
    arr_np = np.asarray([5.0, 0.1, 0.2, 0.3, 0.4, 0.5, 0.1], dtype=np.float32)
    out = _parse_yolo_flat_output(
        arr_np,
        input_hw=(640, 640),
        frame_hw=(640, 640),
        confidence_threshold=0.0,
        accept_classes={0},
    )
    # Should not raise; should return [] or at most what fits cleanly.
    assert isinstance(out, list)
