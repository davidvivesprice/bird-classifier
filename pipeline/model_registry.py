"""Model registry + switcher for the Pi 5 observatory.

The observatory runs exactly ONE primary classifier at a time (the "current"
model). This registry holds N candidate models with metadata + a lazy loader
so we can switch between them at runtime without restarting the pipeline.

Architecture:
  - A CandidateModel is (name, description, type, path, loader).
  - ModelRegistry holds a dict of candidates + knows the current one.
  - `switch(name)` unloads the previous model, loads the new one, takes
    the same lock SmartClassifier uses so inferences-in-flight complete first.
  - `classify(crop_pil)` dispatches to the current model.

Loader contract: each loader returns an object with `.classify(crop_pil) -> list[dict]`
where each dict has at least {common_name: str, scientific_name: str, raw_score: int}.
"""
from __future__ import annotations

import logging
import os
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, List, Optional

log = logging.getLogger(__name__)


@dataclass
class CandidateModel:
    """Metadata + lazy loader for one candidate classifier."""
    name: str
    description: str
    type_: str           # "onnx_cpu" | "hailo" | "tflite_cpu" | "placeholder"
    path: str
    loader: Optional[Callable] = None  # callable(path) -> obj with .classify()
    available: bool = True             # False = shows in UI but disabled
    notes: str = ""                    # extra info for the UI (e.g. "4.3 ms/frame")

    def is_placeholder(self) -> bool:
        return self.type_ == "placeholder" or self.loader is None


@dataclass
class ModelRegistry:
    """Holds N candidates + current model. Thread-safe switch() and classify()."""
    candidates: dict[str, CandidateModel] = field(default_factory=dict)
    current_name: Optional[str] = None
    _current_instance: object = None
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def register(self, candidate: CandidateModel):
        self.candidates[candidate.name] = candidate

    def list(self) -> list[dict]:
        return [
            {
                "name": c.name,
                "description": c.description,
                "type": c.type_,
                "available": c.available and not c.is_placeholder(),
                "active": c.name == self.current_name,
                "notes": c.notes,
            }
            for c in self.candidates.values()
        ]

    def switch(self, name: str) -> dict:
        """Load the named model as current. Returns {ok, current, error?}.

        If the same name is already current, returns ok=True (no-op).
        """
        with self._lock:
            if name not in self.candidates:
                return {"ok": False, "error": f"unknown model: {name}"}
            cand = self.candidates[name]
            if cand.is_placeholder() or not cand.available:
                return {"ok": False, "error": f"not available: {name}"}
            if self.current_name == name and self._current_instance is not None:
                return {"ok": True, "current": name, "noop": True}

            # Load new before releasing old, so classify() always has a valid model
            try:
                new_instance = cand.loader(cand.path)
            except Exception as e:
                log.exception("Failed to load %s: %s", name, e)
                return {"ok": False, "error": f"load failed: {e}"}

            old_instance = self._current_instance
            self._current_instance = new_instance
            self.current_name = name
            # Free old (GC takes care of it; explicit for clarity)
            del old_instance
            log.info("ModelRegistry switched to %s", name)
            return {"ok": True, "current": name, "noop": False}

    def classify(self, crop_pil) -> list[dict]:
        """Classify a PIL image crop with the current model. Returns list of predictions."""
        inst = self._current_instance
        if inst is None:
            return []
        try:
            return inst.classify(crop_pil)
        except Exception as e:
            log.warning("classify failed on %s: %s", self.current_name, e)
            return []


# ── Loaders ───────────────────────────────────────────────────────────────


class _AiyAdapter:
    """Adapter: makes SpeciesClassifier's (filtered, raw)-tuple API match
    the registry's expected list[dict] contract."""

    def __init__(self, impl):
        self.impl = impl

    def classify(self, crop_pil) -> List[dict]:
        filtered, raw = self.impl.classify(crop_pil)
        # Use filtered if regional_species is set, else raw. Same shape.
        return filtered if filtered else raw


def load_aiy_onnx(path: str):
    """Load AIY Birds V1 as an onnxruntime classifier.
    Returns an adapter that exposes `.classify(crop) -> list[dict]`.
    """
    import sys
    repo_root = Path(__file__).resolve().parent.parent
    if str(repo_root) not in sys.path:
        sys.path.insert(0, str(repo_root))
    from bird_inference import SpeciesClassifier
    labels_path = Path(path).parent / "inat_bird_labels.txt"
    impl = SpeciesClassifier(
        model_path=path,
        labels_path=labels_path,
        regional_species=None,
        providers=["CPUExecutionProvider"],
        tpu_model_path=None,
    )
    return _AiyAdapter(impl)


def load_hailo_classifier(path: str):
    """Load a Hailo .hef as a classifier wrapper."""
    # Piggyback on pipeline.hailo_classifier (built below)
    from pipeline.hailo_classifier import HailoClassifier
    return HailoClassifier(path)


# ── Default registry builder (Pi-specific) ────────────────────────────────


def build_default_registry(models_dir: str, exclude_hailo: bool = False) -> ModelRegistry:
    """Build a registry with the candidate set for the Pi observatory.

    - AIY Birds V1 (ONNX CPU) — the primary classifier, benchmarked at 7.4ms.
    - ResNet50 (Hailo) — 1000-class ImageNet, demo of Hailo classification path.
    - YOLOv8s-cls (Hailo) — if available.
    - MobileNet-V2 (Hailo) — if available.
    - Flagship placeholder — pending Tier 2 training, shows as "coming soon".

    `exclude_hailo`: when True, marks all hailo-type candidates as
    `available=False`. Use this in the PIPELINE process which also owns a
    Hailo detector slot — the Hailo-8L has exactly ONE vdevice, so having
    both a Hailo detector AND a Hailo classifier fails with
    HAILO_OUT_OF_PHYSICAL_DEVICES. Dashboard lab upload-test leaves this
    False (classifier runs alone when the Lab is exercised).
    """
    root = Path(models_dir)
    hailo_root = Path("/usr/share/hailo-models")
    reg = ModelRegistry()

    # 1. AIY ONNX on CPU — the baseline, known working
    aiy_onnx = root / "aiy_birds_v1.onnx"
    reg.register(CandidateModel(
        name="aiy_onnx",
        description="AIY Birds V1 — 965 species, ONNX on Pi CPU",
        type_="onnx_cpu",
        path=str(aiy_onnx),
        loader=load_aiy_onnx,
        available=aiy_onnx.exists(),
        notes="7.4 ms/frame (134 FPS) on Pi 5 CPU. Primary classifier.",
    ))

    # 2. ResNet50 on Hailo — 1000-class ImageNet, demonstrates Hailo classification.
    resnet = hailo_root / "resnet_v1_50_h8l.hef"
    _hailo_conflict_note = " · Unavailable while Hailo detector is active (8L has 1 vdevice slot)" if exclude_hailo else ""
    reg.register(CandidateModel(
        name="resnet50_hailo",
        description="ResNet-50 on Hailo — ImageNet (1000 classes, some birds)",
        type_="hailo",
        path=str(resnet),
        loader=load_hailo_classifier,
        available=resnet.exists() and not exclude_hailo,
        notes="ImageNet baseline — demonstrates Hailo classifier path." + _hailo_conflict_note,
    ))

    # 3-4. YOLO-derived Hailo models for coarse "is it a bird at all" signals.
    for h_name, h_desc, h_filename, h_notes in [
        ("yolov8s_hailo",
         "YOLOv8-S on Hailo — COCO 80-class detector",
         "yolov8s_h8l.hef",
         "58.67 FPS, 12.96 ms. Detector, not classifier — shown here for quick comparison."),
        ("yolov6n_hailo",
         "YOLOv6-N on Hailo — COCO 80-class detector (smaller)",
         "yolov6n_h8l.hef",
         "Smaller variant. Detector."),
    ]:
        p = hailo_root / h_filename
        reg.register(CandidateModel(
            name=h_name,
            description=h_desc,
            type_="hailo",
            path=str(p),
            loader=load_hailo_classifier,
            available=p.exists() and not exclude_hailo,
            notes=h_notes + _hailo_conflict_note,
        ))

    # 5. Flagship placeholder — the Tier 2 custom-trained model, not yet built.
    reg.register(CandidateModel(
        name="flagship_pending",
        description="Flagship yard model (trained on our data) — coming soon",
        type_="placeholder",
        path="",
        loader=None,
        available=False,
        notes="Tier 2 training not yet started. 16-class, Hairy/Downy specialist, energy-OOD gate.",
    ))

    # 2026-04-25: honor PI_CLASSIFIER env var for startup selection so
    # /api/models/switch can flip the live pipeline via a service restart.
    # If PI_CLASSIFIER is set AND points at an available candidate, use it.
    # Otherwise fall back to first-available (historical behavior).
    desired = os.environ.get("PI_CLASSIFIER", "").strip()
    if desired and desired in reg.candidates:
        cand = reg.candidates[desired]
        if cand.available and not cand.is_placeholder():
            res = reg.switch(desired)
            if res.get("ok"):
                log.info("ModelRegistry startup: PI_CLASSIFIER=%s → active", desired)
                return reg
            log.warning("ModelRegistry startup: PI_CLASSIFIER=%s failed: %s — falling back",
                        desired, res.get("error"))
        else:
            log.warning("ModelRegistry startup: PI_CLASSIFIER=%s not available — falling back",
                        desired)

    # Fallback: pick the first AVAILABLE non-placeholder. Usually AIY.
    for c in reg.candidates.values():
        if c.available and not c.is_placeholder():
            reg.switch(c.name)
            break

    return reg
