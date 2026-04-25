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

    Lazy-configures on first ``infer`` so unit tests can construct without
    fully exercising the HailoRT runtime.
    """

    def __init__(self, infer_model, hef_path: str):
        self._infer_model = infer_model
        self.hef_path = hef_path
        infer_model.set_batch_size(1)
        self._cim = None  # ConfiguredInferModel, lazy
        self._cim_ctx_entered = False
        self._lock = threading.Lock()

    def _ensure_configured(self):
        if self._cim is not None:
            return
        # Force outputs to FLOAT32 so HailoRT dequantizes internally — keeps
        # callers simple (they always read FLOAT32 logits / NMS coords).
        # Must happen BEFORE configure().
        try:
            import hailo_platform as hp
            for name in self._infer_model.output_names:
                self._infer_model.output(name).set_format_type(hp.FormatType.FLOAT32)
        except Exception as e:
            log.debug("HailoModel output set_format_type FLOAT32 skipped: %s", e)
        cim = self._infer_model.configure()
        cim.__enter__()
        self._cim = cim
        self._cim_ctx_entered = True

    def infer(self, inputs: Dict[str, np.ndarray]) -> Dict[str, np.ndarray]:
        with self._lock:
            self._ensure_configured()
            cim = self._cim
            out_buffers: Dict[str, np.ndarray] = {}
            for name in self._infer_model.output_names:
                spec = self._infer_model.output(name)
                out_buffers[name] = np.empty(spec.shape, dtype=np.float32)
            bindings = cim.create_bindings(output_buffers=out_buffers)
            for name, arr in inputs.items():
                # HailoRT's set_buffer requires a writable C-contiguous buffer.
                # ascontiguousarray returns the input as-is when already
                # C-contiguous, so a read-only source (e.g. PIL→numpy or a
                # negative-stride slice) stays read-only. Force a fresh copy.
                buf = np.ascontiguousarray(arr)
                if not buf.flags.writeable:
                    buf = buf.copy()
                bindings.input(name).set_buffer(buf)
            cim.wait_for_async_ready(timeout_ms=10000)
            job = cim.run_async([bindings])
            job.wait(timeout_ms=10000)
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
                    last_job = getattr(cim, "last_infer_job", None)
                    if last_job is not None:
                        last_job.wait(5000)
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
