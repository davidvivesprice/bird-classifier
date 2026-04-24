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
from typing import Any, List, Optional

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
        self._hef = None
        self._target = None
        self._vdevice = None
        self._network_group = None
        self._input_vstreams_params = None
        self._output_vstreams_params = None
        self._input_shape = (224, 224)  # common default; overridden below
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
        try:
            import hailo_platform as hp
            self._hp = hp
        except Exception as e:
            raise RuntimeError(f"hailo_platform not importable: {e}")

        hp = self._hp
        self._hef = hp.HEF(self.hef_path)
        try:
            self._vdevice = hp.VDevice()
            configure_params = hp.ConfigureParams.create_from_hef(
                hef=self._hef, interface=hp.HailoStreamInterface.PCIe
            )
            self._network_groups = self._vdevice.configure(self._hef, configure_params)
            self._network_group = self._network_groups[0]
            self._network_group_params = self._network_group.create_params()
            self._input_vstreams_params = hp.InputVStreamParams.make_from_network_group(
                self._network_group, quantized=False, format_type=hp.FormatType.FLOAT32,
            )
            self._output_vstreams_params = hp.OutputVStreamParams.make_from_network_group(
                self._network_group, quantized=False, format_type=hp.FormatType.FLOAT32,
            )
            # Extract input shape
            ins = self._hef.get_input_vstream_infos()
            if ins:
                s = ins[0].shape  # (h, w, c) usually
                self._input_shape = (s[1], s[0])  # PIL-order: (w, h)
            log.info("HailoClassifier[%s] ready. Input shape: %s",
                     self.kind, self._input_shape)
        except Exception as e:
            log.exception("Hailo setup failed for %s: %s", self.hef_path, e)
            raise

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
        # Normalize to float32 [0, 1] — matches quantized=False setup
        x = arr.astype(np.float32) / 255.0
        x = x[np.newaxis, ...]  # (1, H, W, 3)

        hp = self._hp
        input_info = list(self._hef.get_input_vstream_infos())[0]
        input_name = input_info.name

        with self._network_group.activate(self._network_group_params):
            with hp.InferVStreams(
                self._network_group,
                self._input_vstreams_params,
                self._output_vstreams_params,
            ) as pipeline:
                output = pipeline.infer({input_name: x})

        # ── Parse output per kind ──────────────────────────────────────
        out_name, out_val = next(iter(output.items()))
        return self._parse_output(self.kind, out_val)

    def _parse_output(self, kind: str, out_val) -> List[dict]:
        arr = np.asarray(out_val).squeeze()
        # classification models typically emit (1000,) or (1, 1000) logits/softmax
        if kind in ("resnet_imagenet", "mobilenet_imagenet"):
            if arr.ndim == 0:
                return []
            # Top-3 indices
            idx = np.argsort(arr)[-3:][::-1]
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
                    "raw_score": int(np.clip(arr[i] * 100, 0, 255)),
                })
            return out

        if kind == "yolo_coco":
            # YOLO output is detection boxes. Not really a classifier, but for
            # the "switcher demo" we surface the top-confidence detected class.
            # Output shape varies; try to handle a (N, 6) [x1,y1,x2,y2,conf,cls]
            # or the raw (N, 85) post-NMS YOLOv8 output.
            if arr.ndim == 2 and arr.shape[1] >= 6:
                # Pick top-confidence row
                scores = arr[:, 4]
                best = int(np.argmax(scores))
                cls = int(arr[best, 5]) if arr.shape[1] > 5 else 0
                if 0 <= cls < len(COCO_LABELS):
                    return [{
                        "common_name": COCO_LABELS[cls],
                        "scientific_name": "",
                        "raw_score": int(np.clip(scores[best] * 100, 0, 255)),
                    }]
            return [{"common_name": "yolo-coco (detector output)",
                     "scientific_name": "", "raw_score": 0}]

        # Generic fallback
        return [{"common_name": f"output {arr.shape}", "scientific_name": "",
                 "raw_score": 0}]

    def close(self):
        try:
            if self._vdevice is not None:
                self._vdevice.release()
        except Exception:
            pass

    def __del__(self):
        self.close()
