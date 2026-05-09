"""bird_inference — shared inference utilities for the bird observatory.

Provides a single source of truth for species aliases, label parsing,
image cropping, and ONNX provider selection.  Imported by classify.py,
the test suite, and dashboard/api.py. (live_detector.py was retired in
the v3 migration; bird_pipeline_v3.py is the production entry point now.)
"""

# ── Species normalisation ──────────────────────────────────────────────────

SPECIES_ALIASES = {
    "Slate-colored Junco": "Dark-eyed Junco",
    "Myrtle Warbler": "Yellow-rumped Warbler",
    "Feral Pigeon": "Rock Pigeon",
    "Yellow-shafted Flicker": "Northern Flicker",
}


def normalize_species(name: str) -> str:
    """Map subspecies/regional form names to canonical species names."""
    return SPECIES_ALIASES.get(name, name)


# ── Label parsing ──────────────────────────────────────────────────────────

def parse_label(raw_label):
    """Split a model label into (scientific_name, common_name).

    Labels have the format "Scientific Name (Common Name)".
    Uses rindex so that species with parentheses in their name — e.g.
    "Hawk (Cooper's) (Accipiter cooperii)" — are split correctly on the
    *last* opening paren.  The classify.py version used split("(")[0]
    which is buggy for such nested cases.
    """
    try:
        idx = raw_label.rindex("(")
        scientific = raw_label[:idx].strip()
        common = raw_label[idx + 1:].rstrip(")")
        return scientific, common
    except ValueError:
        return raw_label, raw_label


# ── Image cropping ─────────────────────────────────────────────────────────

def crop_bird(image, box, pad_ratio=0.15):
    """Crop bird region from image with padding. Accepts PIL Image or numpy HWC array."""
    from PIL import Image as PILImage
    if isinstance(image, PILImage.Image):
        w, h = image.width, image.height
    else:
        h, w = image.shape[:2]
    x1, y1, x2, y2 = box
    bw, bh = x2 - x1, y2 - y1
    pad_x = int(bw * pad_ratio)
    pad_y = int(bh * pad_ratio)
    cx1 = max(0, x1 - pad_x)
    cy1 = max(0, y1 - pad_y)
    cx2 = min(w, x2 + pad_x)
    cy2 = min(h, y2 + pad_y)
    # Guard against zero-size crops (degenerate boxes at image edge)
    if cx2 <= cx1 or cy2 <= cy1:
        cx1, cy1, cx2, cy2 = 0, 0, min(w, 10), min(h, 10)
    if isinstance(image, PILImage.Image):
        return image.crop((cx1, cy1, cx2, cy2))
    return image[cy1:cy2, cx1:cx2]


# ── ONNX provider selection ────────────────────────────────────────────────

def get_providers():
    """Return ONNX Runtime execution providers, preferring CoreML on macOS."""
    try:
        import onnxruntime as ort
        available = ort.get_available_providers()
        if "CoreMLExecutionProvider" in available:
            return ["CoreMLExecutionProvider", "CPUExecutionProvider"]
    except Exception:
        pass
    return ["CPUExecutionProvider"]


# ── YOLO Detection ────────────────────────────────────────────────────────

def _nms(boxes, scores, iou_threshold):
    """Non-maximum suppression in pure numpy.

    boxes: (N, 4) as x1, y1, x2, y2
    scores: (N,)
    Returns list of indices to keep.
    """
    import numpy as np

    if len(boxes) == 0:
        return []

    x1 = boxes[:, 0]
    y1 = boxes[:, 1]
    x2 = boxes[:, 2]
    y2 = boxes[:, 3]
    areas = (x2 - x1) * (y2 - y1)

    order = scores.argsort()[::-1]
    keep = []

    while len(order) > 0:
        i = order[0]
        keep.append(i)
        if len(order) == 1:
            break

        xx1 = np.maximum(x1[i], x1[order[1:]])
        yy1 = np.maximum(y1[i], y1[order[1:]])
        xx2 = np.minimum(x2[i], x2[order[1:]])
        yy2 = np.minimum(y2[i], y2[order[1:]])

        inter = np.maximum(0, xx2 - xx1) * np.maximum(0, yy2 - yy1)
        iou = inter / (areas[i] + areas[order[1:]] - inter + 1e-6)

        remaining = np.where(iou <= iou_threshold)[0]
        order = order[remaining + 1]

    return keep


class YOLODetector:
    """YOLO bird detector wrapping an ONNX model.

    Parameters
    ----------
    model_path : str or Path
        Path to a YOLOv8 ONNX model.
    confidence : float
        Minimum detection confidence (default 0.3).
    iou_threshold : float
        NMS IoU threshold (default 0.45).
    class_id : int or None
        If set, filter for this specific class ID instead of taking the
        max score across all classes.  Default 0 (single-class bird model).
    input_size : int
        Model input resolution (default 640).
    """

    def __init__(self, model_path, *, confidence=0.3, iou_threshold=0.45,
                 class_id=0, input_size=640):
        import onnxruntime as ort

        self.confidence = confidence
        self.iou_threshold = iou_threshold
        self.class_id = class_id
        self.input_size = input_size

        providers = get_providers()
        self._session = ort.InferenceSession(str(model_path), providers=providers)
        self._input_name = self._session.get_inputs()[0].name

    # ── preprocessing ─────────────────────────────────────────────────

    def _preprocess(self, pil_image):
        """Letterbox resize to input_size with gray (114) padding.

        Returns (input_tensor, scale, pad_x, pad_y).
        """
        import numpy as np
        from PIL import Image as PILImage

        orig_w, orig_h = pil_image.size
        size = self.input_size
        scale = min(size / orig_w, size / orig_h)
        new_w = int(orig_w * scale)
        new_h = int(orig_h * scale)

        resized = pil_image.resize((new_w, new_h), PILImage.BILINEAR)

        pad_x = (size - new_w) // 2
        pad_y = (size - new_h) // 2
        padded = PILImage.new("RGB", (size, size), (114, 114, 114))
        padded.paste(resized, (pad_x, pad_y))
        resized.close()  # free intermediate image

        arr = np.array(padded, dtype=np.float32) / 255.0
        padded.close()  # free after converting to numpy
        arr = arr.transpose(2, 0, 1)[np.newaxis]  # NCHW
        return arr, scale, pad_x, pad_y

    # ── public API ────────────────────────────────────────────────────────

    def detect(self, pil_image):
        """Run detection on a PIL Image.

        Returns list of dicts: [{"box": [x1,y1,x2,y2], "confidence": float}, ...]
        Coordinates are in the original image space.
        """
        import numpy as np

        orig_w, orig_h = pil_image.size
        input_tensor, scale, pad_x, pad_y = self._preprocess(pil_image)

        # Run inference — output shape: (1, num_features, num_anchors)
        output = self._session.run(None, {self._input_name: input_tensor})[0]
        predictions = output[0].T  # (num_anchors, num_features)

        boxes_cxcywh = predictions[:, :4]
        class_scores = predictions[:, 4:]

        # Score extraction: single class or max across classes
        if self.class_id is not None:
            scores = class_scores[:, self.class_id]
        else:
            scores = class_scores.max(axis=1)

        # Confidence filter
        mask = scores > self.confidence
        if not mask.any():
            return []

        filtered_boxes = boxes_cxcywh[mask]
        filtered_scores = scores[mask]

        # Convert cx, cy, w, h → x1, y1, x2, y2 (in YOLO input space)
        x1 = filtered_boxes[:, 0] - filtered_boxes[:, 2] / 2
        y1 = filtered_boxes[:, 1] - filtered_boxes[:, 3] / 2
        x2 = filtered_boxes[:, 0] + filtered_boxes[:, 2] / 2
        y2 = filtered_boxes[:, 1] + filtered_boxes[:, 3] / 2
        boxes_xyxy = np.stack([x1, y1, x2, y2], axis=1)

        # NMS
        keep = _nms(boxes_xyxy, filtered_scores, self.iou_threshold)
        if not keep:
            return []

        # Map back to original image coordinates
        detections = []
        for i in keep:
            bx1, by1, bx2, by2 = boxes_xyxy[i]
            ox1 = max(0, min(orig_w, (bx1 - pad_x) / scale))
            oy1 = max(0, min(orig_h, (by1 - pad_y) / scale))
            ox2 = max(0, min(orig_w, (bx2 - pad_x) / scale))
            oy2 = max(0, min(orig_h, (by2 - pad_y) / scale))

            detections.append({
                "box": [int(ox1), int(oy1), int(ox2), int(oy2)],
                "confidence": round(float(filtered_scores[i]), 3),
            })

        return detections


# ── Species Classification ────────────────────────────────────────────────

_SPECIES_INPUT_SIZE = (224, 224)


class SpeciesClassifier:
    """AIY Birds V1 species classifier.

    Tries Coral TPU first (if tpu_model_path provided and pycoral is available),
    then falls back to ONNX.  Input images are resized to 224x224 and fed as
    uint8 (0-255) — NOT normalised floats.

    Parameters
    ----------
    model_path : str or Path
        Path to the AIY Birds V1 ONNX model.
    labels_path : str or Path
        Path to the iNaturalist bird labels text file (one label per line).
    regional_species : set or None
        If provided, ``classify()`` returns a filtered list containing only
        species in this set.  If None, filtered == raw.
    providers : list or None
        ONNX Runtime execution providers.  Defaults to ``get_providers()``.
    tpu_model_path : str, Path, or None
        Path to a Coral Edge TPU ``.tflite`` model.  If the file exists and
        pycoral is importable with a TPU attached, the TPU path is used.
    """

    def __init__(self, model_path, labels_path, regional_species=None,
                 providers=None, tpu_model_path=None):
        with open(labels_path) as f:
            self.labels = [line.strip() for line in f]

        self.regional_species = regional_species
        self._backend = "onnx"  # default

        # --- Try Coral TPU first ------------------------------------------------
        if tpu_model_path is not None:
            from pathlib import Path as _Path
            tpu_path = _Path(tpu_model_path)
            if tpu_path.exists():
                try:
                    from pycoral.utils.edgetpu import list_edge_tpus, make_interpreter
                    from pycoral.adapters import common as coral_common
                    if list_edge_tpus():
                        interp = make_interpreter(str(tpu_path))
                        interp.allocate_tensors()
                        self._session = interp
                        self._coral_common = coral_common
                        self._backend = "coral"
                        self._input_name = None
                        return
                except ImportError:
                    pass
                except Exception:
                    pass

        # --- Fallback: ONNX + CoreML -------------------------------------------
        import onnxruntime as ort

        if providers is None:
            providers = get_providers()
        self._session = ort.InferenceSession(str(model_path), providers=providers)
        self._input_name = self._session.get_inputs()[0].name

    def classify(self, crop):
        """Classify a bird crop image.

        Parameters
        ----------
        crop : PIL.Image.Image or numpy.ndarray
            A cropped bird image (any size — will be resized to 224x224).

        Returns
        -------
        (filtered_predictions, raw_predictions) : tuple[list, list]
            Each prediction is a dict with keys: index, label,
            scientific_name, common_name, raw_score.
            If ``regional_species`` is None, filtered == raw.
        """
        import numpy as np
        from PIL import Image as PILImage

        # Accept both PIL and numpy inputs
        if isinstance(crop, np.ndarray):
            crop = PILImage.fromarray(crop)

        resized = crop.resize(_SPECIES_INPUT_SIZE)
        arr = np.array(resized, dtype=np.uint8)[np.newaxis]  # (1, 224, 224, 3) uint8

        if self._backend == "coral":
            self._coral_common.set_input(self._session, arr[0])
            self._session.invoke()
            scores = np.array(
                self._coral_common.output_tensor(self._session, 0), dtype=np.float32
            )
            if scores.ndim == 2:
                scores = scores[0]
        else:
            scores = self._session.run(None, {self._input_name: arr})[0][0]

        # Raw top 3
        top3_idx = np.argsort(scores)[-3:][::-1]
        raw_predictions = []
        for idx in top3_idx:
            idx = int(idx)
            scientific, common = parse_label(self.labels[idx])
            common = normalize_species(common)
            raw_predictions.append({
                "index": idx,
                "label": self.labels[idx],
                "scientific_name": scientific,
                "common_name": common,
                "raw_score": int(scores[idx]),
            })

        if self.regional_species is None:
            return raw_predictions, raw_predictions

        # Filtered: walk all scores descending, pick top 3 regional matches
        all_idx = np.argsort(scores)[::-1]
        filtered = []
        for idx in all_idx:
            idx = int(idx)
            scientific, common = parse_label(self.labels[idx])
            common = normalize_species(common)
            if common in self.regional_species:
                filtered.append({
                    "index": idx,
                    "label": self.labels[idx],
                    "scientific_name": scientific,
                    "common_name": common,
                    "raw_score": int(scores[idx]),
                })
                if len(filtered) >= 3:
                    break

        if not filtered:
            filtered = [{
                "index": -1,
                "label": "unidentified",
                "scientific_name": "unknown",
                "common_name": "unidentified bird",
                "raw_score": 0,
            }]

        return filtered, raw_predictions
