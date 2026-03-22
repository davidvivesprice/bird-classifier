"""bird_inference — shared inference utilities for the bird observatory.

Provides a single source of truth for species aliases, label parsing,
image cropping, and ONNX provider selection.  Imported by classify.py,
live_detector.py, and dashboard/api.py.
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
