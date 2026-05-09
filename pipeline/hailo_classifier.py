"""HailoClassifier — run a Hailo .hef model as a classifier.

Supports the ResNet-family (ImageNet 1000) and YOLO-family (COCO 80-class)
pre-compiled models shipped in /usr/share/hailo-models. Detects the model
kind from the HEF's output-tensor shape and adapts.

Output shape is {common_name, scientific_name, raw_score} per prediction,
matching SpeciesClassifier so the registry can dispatch uniformly.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import List

import numpy as np

log = logging.getLogger(__name__)


# COCO 80-class label mapping. YOLO detectors output class indices into these.
COCO_LABELS = [
    "person", "bicycle", "car", "motorcycle", "airplane", "bus", "train",
    "truck", "boat", "traffic light", "fire hydrant", "stop sign",
    "parking meter", "bench", "bird", "cat", "dog", "horse", "sheep", "cow",
    "elephant", "bear", "zebra", "giraffe", "backpack", "umbrella",
    "handbag", "tie", "suitcase", "frisbee", "skis", "snowboard",
    "sports ball", "kite", "baseball bat", "baseball glove", "skateboard",
    "surfboard", "tennis racket", "bottle", "wine glass", "cup", "fork",
    "knife", "spoon", "bowl", "banana", "apple", "sandwich", "orange",
    "broccoli", "carrot", "hot dog", "pizza", "donut", "cake", "chair",
    "couch", "potted plant", "bed", "dining table", "toilet", "tv",
    "laptop", "mouse", "remote", "keyboard", "cell phone", "microwave",
    "oven", "toaster", "sink", "refrigerator", "book", "clock", "vase",
    "scissors", "teddy bear", "hair drier", "toothbrush",
]


def _load_imagenet_labels() -> List[str]:
    """Try common paths for ImageNet 1000-class labels. Returns a list (len 1000) or fallback."""
    for p in [
        "/usr/share/hailo-models/imagenet_labels.txt",
        "/usr/share/hailo-models/imagenet_classes.txt",
        Path.home() / "bird-classifier" / "models" / "imagenet_labels.txt",
    ]:
        try:
            with open(p) as f:
                lines = [l.strip() for l in f if l.strip()]
            if len(lines) == 1000:
                return lines
        except Exception:
            continue
    # Fallback: generic "class 0", "class 1", ..., "class 999"
    return [f"class_{i}" for i in range(1000)]


class HailoClassifier:
    """Wrap a Hailo HEF as something that behaves like SpeciesClassifier.

    classify(pil) → list[dict] with {common_name, scientific_name, raw_score}.
    """

    def __init__(self, hef_path: str):
        self.hef_path = hef_path
        self.kind = self._infer_kind(hef_path)
        self.labels = self._labels_for_kind(self.kind)
        self._model = None  # pipeline.hailo_engine.HailoModel
        self._input_shape = (224, 224)  # PIL-order (w, h); overridden below
        self._setup()

    # ── kind detection ─────────────────────────────────────────────────

    @staticmethod
    def _infer_kind(hef_path: str) -> str:
        base = os.path.basename(hef_path).lower()
        if "resnet" in base:
            return "resnet_imagenet"
        if "yolo" in base:
            return "yolo_coco"
        if "mobilenet" in base:
            return "mobilenet_imagenet"
        return "generic"

    def _labels_for_kind(self, kind: str) -> List[str]:
        if kind in ("resnet_imagenet", "mobilenet_imagenet"):
            return _load_imagenet_labels()
        if kind == "yolo_coco":
            return COCO_LABELS
        return [f"class_{i}" for i in range(1000)]

    # ── setup ──────────────────────────────────────────────────────────

    def _setup(self):
        from pipeline.hailo_engine import HailoEngine
        self._model = HailoEngine.get().acquire_model(self.hef_path)
        in_shape = self._model.input_shape()
        if len(in_shape) == 3:
            h, w, _ = in_shape
            self._input_shape = (w, h)  # PIL-order (w, h)
        else:
            self._input_shape = (224, 224)
        log.info("HailoClassifier[%s] ready. Input shape: %s",
                 self.kind, self._input_shape)

    # ── inference ──────────────────────────────────────────────────────

    def classify(self, crop_pil) -> List[dict]:
        """Classify a PIL crop. Returns top-3 predictions."""
        from PIL import Image as PILImage
        import numpy as np

        if isinstance(crop_pil, np.ndarray):
            crop_pil = PILImage.fromarray(crop_pil)
        resized = crop_pil.resize(self._input_shape)
        arr = np.asarray(resized, dtype=np.uint8)
        if arr.ndim == 2:
            arr = np.stack([arr] * 3, axis=-1)
        if arr.shape[-1] == 4:
            arr = arr[..., :3]
        # Pre-compiled HEFs from Hailo's model zoo bake the normalization
        # layer into the graph (e.g. EfficientNet's
        # normalization([127,127,127],[128,128,128]) per playbook §5.1).
        # Pass raw UINT8 0-255 pixels and let the HEF handle it. Matches
        # HailoDetector's UINT8 input contract.
        x = arr[np.newaxis, ...]  # (1, H, W, 3) uint8

        input_name = self._model.input_names[0]
        output = self._model.infer({input_name: x})

        # ── Parse output per kind ──────────────────────────────────────
        out_name, out_val = next(iter(output.items()))
        return self._parse_output(self.kind, out_val)

    def _parse_output(self, kind: str, out_val) -> List[dict]:
        arr = np.asarray(out_val).squeeze()
        # classification models typically emit (1000,) or (1, 1000) logits/softmax
        if kind in ("resnet_imagenet", "mobilenet_imagenet"):
            if arr.ndim == 0:
                return []
            # Hailo's pre-compiled ImageNet HEFs (resnet_v1_50_h8l, etc.) emit
            # raw LOGITS — pre-compiled HEFs typically OMIT the final softmax.
            # Without softmax, `logit * 100` saturates the int16 range almost
            # immediately (a logit of 8 becomes 800 → clipped to 255), making
            # raw_score useless for ranking and turning the downstream
            # confidence-vs-threshold comparison into a binary "saturated or
            # garbage" signal.
            #
            # Fix: numerically-stable softmax → multiply by 255 → int. The
            # raw_score now matches AIY's 0-255 scale where 255 ≈ p=1.0.
            # PiClassifier.classify (pi_classifier.py:56) divides by 255 to
            # produce a true probability for the threshold check.
            #
            # Order is preserved (softmax is monotonic on logits), so
            # top-3 indices below are unchanged.
            #
            # If a HEF ever emits already-softmaxed values, softmax-of-softmax
            # produces a valid (slightly flatter) distribution — order still
            # preserved, threshold may need a small downward retune. Acceptable.
            shifted = arr - np.max(arr)
            exps = np.exp(shifted)
            probs = exps / np.sum(exps)
            # Top-3 indices (sort the probs, but argsort on logits gives same
            # order — softmax is monotonic. Using probs explicitly here for
            # clarity and to make this future-proof if the input shape changes.)
            idx = np.argsort(probs)[-3:][::-1]
            out = []
            for i in idx:
                i = int(i)
                name = self.labels[i] if i < len(self.labels) else f"class_{i}"
                # ImageNet labels are like "n01484850 great white shark"
                # Split off the synset id if present.
                parts = name.split(maxsplit=1)
                common = parts[1] if len(parts) == 2 else parts[0]
                out.append({
                    "common_name": common.replace("_", " "),
                    "scientific_name": "",
                    "raw_score": int(np.clip(probs[i] * 255, 0, 255)),
                })
            return out

        if kind == "yolo_coco":
            # YOLO output is detection boxes. Not really a classifier, but for
            # the "switcher demo" we surface the top-confidence detected class.
            # Output shape varies; try to handle a (N, 6) [x1,y1,x2,y2,conf,cls]
            # or the raw (N, 85) post-NMS YOLOv8 output.
            #
            # YOLO's `conf` value is already a probability in [0, 1] (sigmoid'd
            # objectness × class score), so multiplying by 255 directly gives
            # the same 0-255 scale as AIY/ImageNet softmax above — no logit
            # saturation issue here.
            if arr.ndim == 2 and arr.shape[1] >= 6:
                # Pick top-confidence row
                scores = arr[:, 4]
                best = int(np.argmax(scores))
                cls = int(arr[best, 5]) if arr.shape[1] > 5 else 0
                if 0 <= cls < len(COCO_LABELS):
                    return [{
                        "common_name": COCO_LABELS[cls],
                        "scientific_name": "",
                        "raw_score": int(np.clip(scores[best] * 255, 0, 255)),
                    }]
            return [{"common_name": "yolo-coco (detector output)",
                     "scientific_name": "", "raw_score": 0}]

        # Generic fallback
        return [{"common_name": f"output {arr.shape}", "scientific_name": "",
                 "raw_score": 0}]

    def close(self):
        # Engine owns the VDevice; per-model cleanup happens via
        # HailoEngine.shutdown() at process exit.
        pass
