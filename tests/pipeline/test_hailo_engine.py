"""HailoEngine tests — singleton VDevice + InferModel cache.

Engine tests use a fake `hailo_platform` module so the suite runs on
machines without Hailo HW (iMac dev box). Live HW is exercised via the
bench script (Task 5) and the live Pi restart (Task 7).
"""
from __future__ import annotations

import sys
import types
from unittest.mock import MagicMock

import numpy as np
import pytest


def _install_fake_hailo_platform(monkeypatch):
    """Inject a stand-in `hailo_platform` module so the engine can import
    without real Hailo hardware. Returns the fake for assertion."""
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
    if "pipeline.hailo_engine" in sys.modules:
        del sys.modules["pipeline.hailo_engine"]
    return fake


def test_engine_creates_one_shared_vdevice(monkeypatch):
    fake = _install_fake_hailo_platform(monkeypatch)
    from pipeline import hailo_engine
    hailo_engine.HailoEngine._reset_for_testing()

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
    assert m1 is m2, "Same HEF path must return the same handle (caching)"
    assert m3 is not m1
    vd = fake._created_vdevices[0]
    assert len(vd._models) == 2, (
        f"Should have created 2 InferModels (one per unique path), got {len(vd._models)}"
    )


def test_engine_shutdown_releases_vdevice(monkeypatch):
    fake = _install_fake_hailo_platform(monkeypatch)
    from pipeline import hailo_engine
    hailo_engine.HailoEngine._reset_for_testing()

    eng = hailo_engine.HailoEngine.get()
    eng.acquire_model("/x.hef")
    eng.shutdown()
    assert fake._created_vdevices[0].released is True


def test_engine_reset_for_testing_drops_singleton(monkeypatch):
    fake = _install_fake_hailo_platform(monkeypatch)
    from pipeline import hailo_engine

    hailo_engine.HailoEngine._reset_for_testing()
    eng1 = hailo_engine.HailoEngine.get()
    hailo_engine.HailoEngine._reset_for_testing()
    eng2 = hailo_engine.HailoEngine.get()
    assert eng1 is not eng2, "_reset_for_testing must force a fresh singleton"
    assert len(fake._created_vdevices) == 2
