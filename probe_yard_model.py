#!/usr/bin/env python3
"""probe_yard_model.py — One-time verification script for yard model integration.

Verifies all assumptions before writing production code:
1. Label collisions after normalize_species()
2. Two interpreters on one Coral USB (AIY + yard model)
3. Score ranges for both models on real images
4. 'not a bird' class index and behavior
5. Score distributions to help set confidence thresholds

Run with:
    /Users/vives/bird-classifier/venv-coral/bin/python probe_yard_model.py
"""

# ══════════════════════════════════════════════════════════════════════════════
# PROBE RESULTS (recorded 2026-03-28, venv-coral Python 3.9, Coral USB v1)
# ══════════════════════════════════════════════════════════════════════════════
#
# PROBE 1 — Label collisions:
#   COLLISION: 'Dark-eyed Junco'  class_id=13 ('Dark-eyed Junco') + class_id=37 ('Slate-colored Junco')
#   COLLISION: 'Rock Pigeon'      class_id=17 ('Feral Pigeon')    + class_id=33 ('Rock Pigeon')
#   'not a bird' is class_id=43 and normalize_species() leaves it unchanged.
#   NOTE: 'Lincolns Sparrow' (class_id=25) has no apostrophe — won't alias anything.
#
# PROBE 2 — Two interpreters on one Coral USB:
#   CONFIRMED: Both AIY (965 classes) and yard model (44 classes) load and run on
#   the same Coral USB simultaneously. Alternating inference rounds all succeed.
#   AIY load time: ~3045ms (first load, firmware flash). Yard load: ~6ms.
#   IMPORTANT: The Coral USB can only be held by one OS process at a time.
#   Running probe while classify.py is running causes "Failed to load delegate
#   from libedgetpu.1.dylib" — this is a process-level lock, not a model-level limit.
#
# PROBE 3 — Score ranges (Coral TPU output):
#   AIY model:  dtype=float32, shape=(965,), integer values 0-255 range, but
#               in practice max observed=151 across 10 images. Sum ~200-225/image.
#               Spec assumption of "0-255 integers" is WRONG — it's float32 with
#               integer values, not uint8. The current code does int(scores[idx])
#               which works but loses precision. Effective range: 0-151 observed.
#   Yard model: dtype=float32, shape=(44,), integer values 3-12 observed.
#               Spec assumption of "0.0-1.0 floats" is WRONG. The yard model
#               outputs the SAME scale as AIY (small integers, not 0-1 floats).
#               This is a weight-imprinting MobileNet on Coral — output is raw
#               logits/scores, NOT softmax probabilities.
#
# PROBE 4 — 'not a bird' class (class_id=43):
#   Blank gray image: 'not a bird' scored 7.0, tied for first with Northern Flicker.
#   Background images: 'not a bird' scored 4-5, NOT top prediction. Scores flat.
#   Real bird image: 'not a bird' scored 4.0, NOT top prediction.
#   CONCLUSION: The 'not a bird' class does NOT reliably signal non-bird inputs.
#   Score range for all classes is very narrow (3-12). Cannot use it as a gate.
#
# PROBE 5 — Score distributions:
#   AIY:  min=0, max=151, mean=0.22, p99=3. Scores are VERY sparse.
#         Top-1 winning scores: 19-151. No scores > 151 in this sample.
#         Fraction > 100: 0.03% — very few classes ever score high.
#   Yard: min=3, max=12, mean=5.74, p99=10. ALL values above 0.
#         Max winning score = 12. Scores are compressed into range 3-12.
#         The yard model output is NOT softmax-normalized. Differences are small.
#
# KEY FINDINGS FOR TASKS 2-3:
#   1. Coral lock: Only one process can hold the Coral at a time. YardClassifier
#      must share the Coral lock with the existing AIY interpreter, not own it.
#   2. Score scale: BOTH models output float32 integer-valued scores (not 0-1).
#      Yard scores are very compressed (3-12). A threshold of e.g. score >= 10
#      or score >= (max - 2) may be needed rather than a fixed absolute threshold.
#   3. Collision handling: For 'Dark-eyed Junco' and 'Rock Pigeon', the
#      YardClassifier must SUM scores across colliding class_ids before ranking.
#   4. 'not a bird' gate: Unreliable as a binary gate. Must use it differently —
#      perhaps only accept yard result if top score is clearly above others,
#      or if 'not a bird' is NOT the top prediction.
#   5. output_tensor returns shape (1, N) — always call .flatten() before use.
#
# ══════════════════════════════════════════════════════════════════════════════

import sys
import os
import time
from pathlib import Path

# Add project root to path so we can import bird_inference
PROJECT_ROOT = Path(__file__).parent
sys.path.insert(0, str(PROJECT_ROOT))

MODEL_DIR = PROJECT_ROOT / "models"
YARD_MODEL   = MODEL_DIR / "yard_model.tflite"
YARD_LABELS  = MODEL_DIR / "yard_model_labels.txt"
AIY_TPU      = MODEL_DIR / "aiy_birds_v1_edgetpu.tflite"
AIY_ONNX     = MODEL_DIR / "aiy_birds_v1.onnx"
AIY_LABELS   = MODEL_DIR / "inat_bird_labels.txt"
YOLO_MODEL   = MODEL_DIR / "yolov8n_bird.onnx"
REGIONAL     = MODEL_DIR / "chilmark_feeder_species.txt"

CLASSIFIED_DIR = Path("/Users/vives/bird-snapshots/classified")

# 10 real bird images from different species dirs (seed=42 selection)
TEST_IMAGES = [
    CLASSIFIED_DIR / "Purple_Finch/ground_2026-03-16_17-53-47.jpg",
    CLASSIFIED_DIR / "Red-breasted_Nuthatch/ground_2026-03-16_12-07-35.jpg",
    CLASSIFIED_DIR / "Brown_Creeper/2026-03-11_14-34-37.jpg",
    CLASSIFIED_DIR / "Savannah_Sparrow/ground_2026-03-15_15-48-15.jpg",
    CLASSIFIED_DIR / "Eastern_Towhee/ground_2026-03-17_11-43-40.jpg",
    CLASSIFIED_DIR / "Brown_Thrasher/ground_2026-03-17_08-09-14.jpg",
    CLASSIFIED_DIR / "Feral_Pigeon/ground_2026-03-18_08-54-19.jpg",
    CLASSIFIED_DIR / "European_Starling/feeder_2026-03-17_13-14-13.jpg",
    CLASSIFIED_DIR / "Rock_Pigeon/ground_2026-03-19_23-45-01.jpg",
    CLASSIFIED_DIR / "Hermit_Thrush/ground_2026-03-19_08-40-43.jpg",
]

SEP = "─" * 70


def section(title):
    print(f"\n{'═'*70}")
    print(f"  {title}")
    print(f"{'═'*70}")


def subsection(title):
    print(f"\n{SEP}")
    print(f"  {title}")
    print(SEP)


# ── PROBE 1: Label collisions ──────────────────────────────────────────────

def probe_label_collisions():
    section("PROBE 1: Label collisions after normalize_species()")

    from bird_inference import normalize_species

    with open(YARD_LABELS) as f:
        yard_labels = [l.strip() for l in f if l.strip()]

    print(f"\nYard model has {len(yard_labels)} labels:")
    for i, lbl in enumerate(yard_labels):
        canonical = normalize_species(lbl)
        marker = "  <-- ALIASED" if canonical != lbl else ""
        print(f"  [{i:2d}] {lbl!r:35s}  ->  {canonical!r}{marker}")

    # Find collisions: multiple raw labels map to same canonical name
    from collections import defaultdict
    canon_to_ids = defaultdict(list)
    for i, lbl in enumerate(yard_labels):
        canonical = normalize_species(lbl)
        canon_to_ids[canonical].append((i, lbl))

    print("\nCollisions (canonical name shared by 2+ yard labels):")
    found_any = False
    for canonical, entries in canon_to_ids.items():
        if len(entries) > 1:
            found_any = True
            print(f"  COLLISION: {canonical!r}")
            for idx, raw in entries:
                print(f"    class_id={idx}  raw_label={raw!r}")
    if not found_any:
        print("  (none)")

    # Also check: does 'not a bird' normalize to something different?
    nab_indices = [i for i, l in enumerate(yard_labels) if l.lower() == "not a bird"]
    print(f"\n'not a bird' class indices in yard model: {nab_indices}")
    if nab_indices:
        print(f"  normalize_species('not a bird') -> {normalize_species('not a bird')!r}")

    return yard_labels


# ── PROBE 2: Two interpreters on one Coral ─────────────────────────────────

def probe_dual_coral():
    section("PROBE 2: Two interpreters on one Coral USB")

    import numpy as np

    try:
        from pycoral.utils.edgetpu import list_edge_tpus, make_interpreter
        from pycoral.adapters import common as coral_common
    except ImportError as e:
        print(f"  BLOCKED: pycoral not importable: {e}")
        return None, None

    tpus = list_edge_tpus()
    print(f"\nDetected TPUs: {tpus}")

    if not tpus:
        print("  BLOCKED: No Coral USB found")
        return None, None

    # Load AIY model
    print(f"\nLoading AIY TPU model: {AIY_TPU}")
    t0 = time.perf_counter()
    try:
        aiy_interp = make_interpreter(str(AIY_TPU))
        aiy_interp.allocate_tensors()
        dt_aiy = time.perf_counter() - t0
        print(f"  AIY interpreter loaded in {dt_aiy*1000:.1f}ms")
    except Exception as e:
        print(f"  FAILED to load AIY: {e}")
        return None, None

    # Load yard model (second interpreter, same Coral)
    print(f"\nLoading yard model on same Coral: {YARD_MODEL}")
    t0 = time.perf_counter()
    try:
        yard_interp = make_interpreter(str(YARD_MODEL))
        yard_interp.allocate_tensors()
        dt_yard = time.perf_counter() - t0
        print(f"  Yard interpreter loaded in {dt_yard*1000:.1f}ms")
    except Exception as e:
        print(f"  FAILED to load yard model: {e}")
        return aiy_interp, None

    # Run dummy inference on each to confirm both work
    print("\nRunning dummy inference on AIY interpreter...")
    dummy_input = np.zeros((1, 224, 224, 3), dtype=np.uint8)
    t0 = time.perf_counter()
    coral_common.set_input(aiy_interp, dummy_input[0])
    aiy_interp.invoke()
    aiy_out = np.array(coral_common.output_tensor(aiy_interp, 0), dtype=np.float32)
    dt = time.perf_counter() - t0
    print(f"  AIY dummy output shape: {aiy_out.shape}, dtype: {aiy_out.dtype}, "
          f"range: [{aiy_out.min():.3f}, {aiy_out.max():.3f}], time: {dt*1000:.1f}ms")

    print("\nRunning dummy inference on yard interpreter...")
    t0 = time.perf_counter()
    coral_common.set_input(yard_interp, dummy_input[0])
    yard_interp.invoke()
    yard_out = np.array(coral_common.output_tensor(yard_interp, 0), dtype=np.float32)
    dt = time.perf_counter() - t0
    print(f"  Yard dummy output shape: {yard_out.shape}, dtype: {yard_out.dtype}, "
          f"range: [{yard_out.min():.3f}, {yard_out.max():.3f}], time: {dt*1000:.1f}ms")

    # Alternate between the two interpreters several times
    print("\nAlternating between interpreters 5 times each...")
    for i in range(5):
        coral_common.set_input(aiy_interp, dummy_input[0])
        aiy_interp.invoke()
        aiy_tmp = np.array(coral_common.output_tensor(aiy_interp, 0), dtype=np.float32)

        coral_common.set_input(yard_interp, dummy_input[0])
        yard_interp.invoke()
        yard_tmp = np.array(coral_common.output_tensor(yard_interp, 0), dtype=np.float32)

        print(f"  round {i+1}: AIY max={aiy_tmp.max():.3f}, yard max={yard_tmp.max():.3f}  OK")

    print("\n  RESULT: Both interpreters coexist on one Coral USB")
    return aiy_interp, yard_interp


# ── PROBE 3 & 5: Score ranges on real images ──────────────────────────────

def probe_score_ranges(aiy_interp, yard_interp):
    section("PROBE 3 & 5: Score ranges on real images + score distributions")

    import numpy as np
    from PIL import Image

    try:
        from pycoral.utils.edgetpu import list_edge_tpus, make_interpreter
        from pycoral.adapters import common as coral_common
    except ImportError as e:
        print(f"  BLOCKED: pycoral not importable: {e}")
        return

    with open(AIY_LABELS) as f:
        aiy_labels = [l.strip() for l in f]
    with open(YARD_LABELS) as f:
        yard_labels = [l.strip() for l in f]

    # Check if AIY interpreter is available; if not, fall back to ONNX
    using_aiy_tpu = aiy_interp is not None
    if not using_aiy_tpu:
        print("  AIY TPU not available, using ONNX fallback for AIY")
        import onnxruntime as ort
        aiy_session = ort.InferenceSession(str(AIY_ONNX), providers=["CPUExecutionProvider"])
        aiy_input_name = aiy_session.get_inputs()[0].name

    using_yard_tpu = yard_interp is not None
    if not using_yard_tpu:
        print("  Yard TPU not available, cannot test yard model")

    all_aiy_scores = []
    all_yard_scores = []

    for img_path in TEST_IMAGES:
        img_path = Path(img_path)
        species_dir = img_path.parent.name.replace("_", " ")

        if not img_path.exists():
            print(f"\n  [MISSING] {img_path}")
            continue

        try:
            img = Image.open(img_path).convert("RGB")
        except Exception as e:
            print(f"\n  [ERROR opening {img_path.name}]: {e}")
            continue

        resized = img.resize((224, 224))
        arr = np.array(resized, dtype=np.uint8)

        subsection(f"Image: {img_path.name}  (species dir: {species_dir})")
        print(f"  Image size: {img.size}")

        # ── AIY inference ──
        if using_aiy_tpu:
            t0 = time.perf_counter()
            coral_common.set_input(aiy_interp, arr)
            aiy_interp.invoke()
            raw_scores = np.array(coral_common.output_tensor(aiy_interp, 0), dtype=np.float32).flatten()
            dt = time.perf_counter() - t0
        else:
            t0 = time.perf_counter()
            raw_scores = aiy_session.run(None, {aiy_input_name: arr[np.newaxis]})[0][0]
            dt = time.perf_counter() - t0

        all_aiy_scores.extend(raw_scores.tolist())
        top3_aiy = np.argsort(raw_scores)[-3:][::-1]
        print(f"\n  AIY scores — shape:{raw_scores.shape} dtype:{raw_scores.dtype} "
              f"min:{raw_scores.min():.3f} max:{raw_scores.max():.3f} "
              f"sum:{raw_scores.sum():.3f} time:{dt*1000:.1f}ms")
        print(f"  AIY top-3:")
        for rank, idx in enumerate(top3_aiy):
            idx_i = int(idx)
            lbl = aiy_labels[idx_i] if idx_i < len(aiy_labels) else f"<OOB:{idx_i}>"
            print(f"    [{rank+1}] idx={idx_i:4d}  score={raw_scores[idx_i]:.3f}  {lbl!r}")

        # ── Yard inference ──
        if using_yard_tpu:
            t0 = time.perf_counter()
            coral_common.set_input(yard_interp, arr)
            yard_interp.invoke()
            yard_scores = np.array(coral_common.output_tensor(yard_interp, 0), dtype=np.float32).flatten()
            dt = time.perf_counter() - t0

            all_yard_scores.extend(yard_scores.tolist())
            top3_yard = np.argsort(yard_scores)[-3:][::-1]
            print(f"\n  Yard scores — shape:{yard_scores.shape} dtype:{yard_scores.dtype} "
                  f"min:{yard_scores.min():.4f} max:{yard_scores.max():.4f} "
                  f"sum:{yard_scores.sum():.4f} time:{dt*1000:.1f}ms")
            print(f"  Yard top-3:")
            for rank, idx in enumerate(top3_yard):
                idx_i = int(idx)
                lbl = yard_labels[idx_i] if idx_i < len(yard_labels) else f"<OOB:{idx_i}>"
                print(f"    [{rank+1}] idx={idx_i:2d}  score={yard_scores[idx_i]:.4f}  {lbl!r}")

    # ── Score distribution summary ──
    section("PROBE 5: Score distribution summary")

    if all_aiy_scores:
        aiy_arr = np.array(all_aiy_scores)
        print(f"\nAIY score distribution ({len(all_aiy_scores)} values across {len(TEST_IMAGES)} images):")
        print(f"  min={aiy_arr.min():.3f}  max={aiy_arr.max():.3f}  mean={aiy_arr.mean():.3f}")
        print(f"  p50={np.percentile(aiy_arr, 50):.3f}  p90={np.percentile(aiy_arr, 90):.3f}  "
              f"p95={np.percentile(aiy_arr, 95):.3f}  p99={np.percentile(aiy_arr, 99):.3f}")
        print(f"  Fraction > 100:  {(aiy_arr > 100).mean()*100:.2f}%")
        print(f"  Fraction > 150:  {(aiy_arr > 150).mean()*100:.2f}%")
        print(f"  Fraction > 200:  {(aiy_arr > 200).mean()*100:.2f}%")
        print(f"  Fraction > 230:  {(aiy_arr > 230).mean()*100:.2f}%")

    if all_yard_scores:
        yard_arr = np.array(all_yard_scores)
        print(f"\nYard score distribution ({len(all_yard_scores)} values across {len(TEST_IMAGES)} images):")
        print(f"  min={yard_arr.min():.4f}  max={yard_arr.max():.4f}  mean={yard_arr.mean():.4f}")
        print(f"  p50={np.percentile(yard_arr, 50):.4f}  p90={np.percentile(yard_arr, 90):.4f}  "
              f"p95={np.percentile(yard_arr, 95):.4f}  p99={np.percentile(yard_arr, 99):.4f}")
        print(f"  Fraction > 0.5:  {(yard_arr > 0.5).mean()*100:.2f}%")
        print(f"  Fraction > 0.7:  {(yard_arr > 0.7).mean()*100:.2f}%")
        print(f"  Fraction > 0.9:  {(yard_arr > 0.9).mean()*100:.2f}%")
        print(f"  Fraction > 0.95: {(yard_arr > 0.95).mean()*100:.2f}%")


# ── PROBE 4: 'not a bird' class behavior ──────────────────────────────────

def probe_not_a_bird(yard_interp):
    section("PROBE 4: 'not a bird' class index and behavior")

    import numpy as np
    from PIL import Image

    with open(YARD_LABELS) as f:
        yard_labels = [l.strip() for l in f]

    nab_indices = [i for i, l in enumerate(yard_labels) if l.lower() == "not a bird"]
    print(f"\n'not a bird' index in yard model labels: {nab_indices}")
    if nab_indices:
        print(f"  Label text: {yard_labels[nab_indices[0]]!r}")

    if yard_interp is None:
        print("  Yard interpreter not available, skipping live inference test")
        return

    try:
        from pycoral.adapters import common as coral_common
    except ImportError as e:
        print(f"  BLOCKED: {e}")
        return

    def _yard_infer(arr):
        coral_common.set_input(yard_interp, arr)
        yard_interp.invoke()
        return np.array(coral_common.output_tensor(yard_interp, 0), dtype=np.float32).flatten()

    def _print_yard_top3(scores):
        top3 = np.argsort(scores)[-3:][::-1]
        if nab_indices:
            print(f"  'not a bird' score: {scores[nab_indices[0]]:.4f}")
        print("  Top-3 predictions:")
        for rank, idx in enumerate(top3):
            idx_i = int(idx)
            lbl = yard_labels[idx_i] if idx_i < len(yard_labels) else f"<OOB:{idx_i}>"
            print(f"    [{rank+1}] idx={idx_i:2d}  score={scores[idx_i]:.4f}  {lbl!r}")

    # Test 1: blank (all-gray) image — should score high on 'not a bird'
    print("\nTest: blank gray image (128,128,128)...")
    blank = np.full((224, 224, 3), 128, dtype=np.uint8)
    _print_yard_top3(_yard_infer(blank))

    # Test 2: an actual background/non-bird image if available
    bg_dirs = list(CLASSIFIED_DIR.glob("background"))
    if bg_dirs:
        bg_images = list(bg_dirs[0].glob("*.jpg"))[:3]
        for bg_img in bg_images:
            print(f"\nTest: background image {bg_img.name}...")
            img = Image.open(bg_img).convert("RGB").resize((224, 224))
            arr = np.array(img, dtype=np.uint8)
            _print_yard_top3(_yard_infer(arr))
    else:
        print("\n  (No background/ dir found, skipping non-bird image test)")

    # Test 3: a real bird image — 'not a bird' should score low
    bird_img_path = TEST_IMAGES[0]
    if Path(bird_img_path).exists():
        print(f"\nTest: real bird image {Path(bird_img_path).name}...")
        img = Image.open(bird_img_path).convert("RGB").resize((224, 224))
        arr = np.array(img, dtype=np.uint8)
        _print_yard_top3(_yard_infer(arr))


# ── Main ───────────────────────────────────────────────────────────────────

def main():
    print("=" * 70)
    print("  PROBE: Yard Model Integration Assumptions")
    print(f"  Run at: {time.strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 70)

    # Verify key files exist
    section("File existence check")
    for label, path in [
        ("YARD_MODEL",  YARD_MODEL),
        ("YARD_LABELS", YARD_LABELS),
        ("AIY_TPU",     AIY_TPU),
        ("AIY_ONNX",    AIY_ONNX),
        ("AIY_LABELS",  AIY_LABELS),
        ("YOLO_MODEL",  YOLO_MODEL),
        ("REGIONAL",    REGIONAL),
    ]:
        status = "OK" if Path(path).exists() else "MISSING"
        print(f"  [{status}] {label}: {path}")

    # Run probes
    yard_labels = probe_label_collisions()
    aiy_interp, yard_interp = probe_dual_coral()
    probe_score_ranges(aiy_interp, yard_interp)
    probe_not_a_bird(yard_interp)

    section("PROBE COMPLETE")
    print()


if __name__ == "__main__":
    main()
