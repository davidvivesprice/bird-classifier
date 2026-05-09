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
    """Metadata + lazy loader for one candidate classifier.

    UX convention (David: "simple at a glance, deep if you investigate"):
    - ``description`` + ``notes`` are the at-a-glance copy shown in the
      Model Lab row. Keep them ONE phrase each, no duplication.
    - ``info`` is the lightbox/modal body — multi-paragraph detail on
      what the model is, how it runs on the Pi, and why we keep it as
      a candidate. Plain text, blank lines between paragraphs.
    """
    name: str
    description: str
    type_: str           # "onnx_cpu" | "hailo" | "tflite_cpu" | "placeholder"
    path: str
    loader: Optional[Callable] = None  # callable(path) -> obj with .classify()
    available: bool = True             # False = shows in UI but disabled
    notes: str = ""                    # one-phrase metadata for the UI row
    info: str = ""                     # multi-paragraph deep-dive for the
                                       # info-icon lightbox
    is_classifier: bool = True         # False = detector (COCO classes) — not
                                       # selectable as the live pipeline
                                       # classifier (would emit COCO labels
                                       # like "bird"/"cat" instead of species).
                                       # Still loadable via Lab upload-test.

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
                "info": c.info,
                "is_classifier": c.is_classifier,
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


def load_aiy_onnx(path: str, regional_species=None):
    """Load AIY Birds V1 as an onnxruntime classifier.
    Returns an adapter that exposes `.classify(crop) -> list[dict]`.

    regional_species: set of common-name strings to constrain predictions.
      When set, classify() walks all 965 scores descending and returns the
      top regional match rather than the raw top-1. Pass the chilmark species
      set to suppress impossible species (Altamira Oriole, Carolina Chickadee,
      etc.). None = raw model output, no geographic filtering.
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
        regional_species=regional_species,
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


def build_default_registry(models_dir: str, regional_species=None) -> ModelRegistry:
    """Build a registry with the candidate set for the Pi observatory.

    - AIY Birds V1 (ONNX CPU) — the primary classifier, benchmarked at 7.4ms.
    - ResNet50 (Hailo) — 1000-class ImageNet, demo of Hailo classification path.
    - YOLOv8s-cls (Hailo) — detector candidate, lab-only.
    - YOLOv6n (Hailo) — detector candidate, lab-only.
    - Flagship placeholder — pending Tier 2 training, shows as "coming soon".

    Hailo candidates are marked `available` purely on HEF presence; the
    Hailo-8L's single VDevice slot is shared via HailoEngine + the
    HailoRT scheduler (see pipeline/hailo_engine.py and playbook §9
    Path 1), so detector + classifier can coexist in one process.

    regional_species: set of common names from chilmark_feeder_species.txt.
      Passed to the AIY ONNX loader so impossible species (Altamira Oriole,
      Carolina Chickadee, etc.) are filtered at inference time rather than
      stored as misclassifications. None = no geographic filtering (not
      recommended for production).
    """
    import functools
    root = Path(models_dir)
    hailo_root = Path("/usr/share/hailo-models")
    reg = ModelRegistry()

    # AIY loader baked with the regional species set (or None for raw).
    # functools.partial so the CandidateModel loader signature stays (path,).
    aiy_loader = functools.partial(load_aiy_onnx, regional_species=regional_species)

    # 1. AIY Birds V1 (ONNX on CPU) — the primary classifier.
    aiy_onnx = root / "aiy_birds_v1.onnx"
    reg.register(CandidateModel(
        name="aiy_onnx",
        description="AIY Birds V1 · 965 bird species",
        type_="onnx_cpu",
        path=str(aiy_onnx),
        loader=aiy_loader,
        available=aiy_onnx.exists(),
        notes="ONNX on Pi CPU · 7.4 ms / crop · primary",
        info=(
            "Google's AIY Birds V1 — a MobileNet-V1 trained on iNaturalist "
            "Birds, outputting probabilities across 965 species.\n\n"
            "How it runs on the Pi: ONNX runtime on the CPU. Each bird-region "
            "crop costs ~7.4 ms (~134 FPS) — well above the pipeline's 5 FPS "
            "detection rate, so we never wait on it.\n\n"
            "Why it's primary: best species coverage on the planet for North "
            "American birds, and fast enough on CPU that we don't compete "
            "with the YOLOv8s detector for the Hailo NPU slot.\n\n"
            "The label you see in the Live view (e.g. 'Carolina Chickadee · "
            "60%') is this model's top prediction on the bbox YOLOv8s detected."
        ),
    ))

    # 2. ResNet-50 on Hailo — 1000-class ImageNet baseline.
    resnet = hailo_root / "resnet_v1_50_h8l.hef"
    reg.register(CandidateModel(
        name="resnet50_hailo",
        description="ResNet-50 · ImageNet 1000 classes",
        type_="hailo",
        path=str(resnet),
        loader=load_hailo_classifier,
        available=resnet.exists(),
        notes="Hailo NPU · baseline · few birds in classes",
        info=(
            "The classic 50-layer residual CNN, trained on ImageNet's 1000 "
            "classes — only ~50 of which are bird species, mostly Eurasian.\n\n"
            "How it runs on the Pi: compiled as a Hailo HEF and run on the "
            "NPU. Cohabits with the YOLOv8s detector on the same Hailo-8L "
            "VDevice via the HailoRT ROUND_ROBIN scheduler (see playbook §9 "
            "Path 1). Per-call latency ~21 ms when co-scheduled.\n\n"
            "Why it's here: demonstrates the Hailo classifier path works in "
            "production, and serves as a sanity check before we drop our "
            "Tier 2 flagship onto the same path. Not great for fine-grained "
            "North American bird ID — switching the live pipeline to it "
            "makes labels read like 'magpie' or 'great grey owl'."
        ),
    ))

    # 3-4. YOLO-derived Hailo models. These are DETECTORS (COCO 80-class),
    # exposed here for Lab upload-test only. is_classifier=False blocks the
    # live-classifier slot from being set to one — picking them would emit
    # COCO labels ("bird"/"cat") instead of species names.
    yolo_specs = [
        {
            "name": "yolov8s_hailo",
            "description": "YOLOv8-S · COCO 80-class detector",
            "filename": "yolov8s_h8l.hef",
            "notes": "Hailo NPU · 17 ms · Lab only",
            "info": (
                "YOLOv8-Small — Ultralytics' object detector trained on COCO's "
                "80 classes. THIS is the detector running constantly in the "
                "live pipeline; the bbox you see in the Live view is its "
                "output.\n\n"
                "How it runs on the Pi: pre-compiled HEF with NMS baked in, "
                "on the Hailo-8L NPU at ~17 ms / frame (~58 FPS) when alone, "
                "~22 ms (~45 FPS) when co-scheduled with a Hailo classifier.\n\n"
                "Why it's listed under classifiers: as a Lab upload-test "
                "convenience — feeding it a single image returns the "
                "highest-confidence COCO class found (almost always 'bird' "
                "on a feeder shot). The 'DETECTOR · LAB ONLY' badge means we "
                "don't allow switching the live pipeline to it: you'd get "
                "'bird' as your label instead of a species."
            ),
        },
        {
            "name": "yolov6n_hailo",
            "description": "YOLOv6-N · COCO 80-class detector (smaller)",
            "filename": "yolov6n_h8l.hef",
            "notes": "Hailo NPU · smaller variant · Lab only",
            "info": (
                "YOLOv6-Nano — Meituan's smaller, faster YOLO variant, also "
                "trained on COCO's 80 classes.\n\n"
                "How it runs on the Pi: same Hailo path as YOLOv8-S but "
                "smaller — faster inference, slightly less accurate.\n\n"
                "Why it's here: alternative detector candidate for benchmark "
                "comparison. We keep YOLOv8-S as the live detector because "
                "per-frame budget isn't the bottleneck — the sub-stream is "
                "already capped at 5 FPS for downstream classification."
            ),
        },
    ]
    for s in yolo_specs:
        p = hailo_root / s["filename"]
        reg.register(CandidateModel(
            name=s["name"],
            description=s["description"],
            type_="hailo",
            path=str(p),
            loader=load_hailo_classifier,
            available=p.exists(),
            notes=s["notes"],
            info=s["info"],
            is_classifier=False,
        ))

    # 5. Flagship placeholder — the Tier 2 custom-trained model, not yet built.
    reg.register(CandidateModel(
        name="flagship_pending",
        description="Flagship yard model · coming soon",
        type_="placeholder",
        path="",
        loader=None,
        available=False,
        notes="Tier 2 · not yet trained",
        info=(
            "The model we're building specifically for our feeder, replacing "
            "AIY's 965 generic species with a tight set tuned to what we "
            "actually see.\n\n"
            "Plan:\n"
            "• 16 classes (the species in our snapshot history, no long tail)\n"
            "• Hairy/Downy woodpecker specialist head — those two get "
            "confused often and a dedicated head can use the bill-length "
            "and back-pattern features the main head dilutes\n"
            "• Energy-based OOD gate so 'this is not a bird I know' is a "
            "first-class output, not a low-confidence guess\n"
            "• EfficientNet-Lite0 backbone — Hailo-Zoo first-class, baked-"
            "in normalization, ~2 ms / crop on Hailo\n\n"
            "Status: dataset audit in progress. Not yet trained. See "
            "project_yard_model_revamp.md and the tier2_eval/ harness."
        ),
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
