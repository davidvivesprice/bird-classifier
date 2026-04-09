#!/usr/bin/env python3
"""Local training script — runs the same pipeline as the Colab notebook.

Usage:
    python train_local.py path/to/training_export.zip
    python train_local.py path/to/training_export.zip --epochs 15
"""

import argparse
import json
import logging
import os
import shutil
import sys
import tempfile
import zipfile
from pathlib import Path

import numpy as np

os.environ["TF_CPP_MIN_LOG_LEVEL"] = "2"  # Reduce TF noise
import tensorflow as tf
import tensorflow_hub as hub

log = logging.getLogger(__name__)

MODELS_DIR = Path(__file__).parent / "models"
IMG_SIZE = 224
BATCH_SIZE = 32
CONFIDENCE_THRESHOLD = 0.45


def find_data_root(extract_dir):
    """Find the directory containing manifest.json inside the extracted zip."""
    if (Path(extract_dir) / "manifest.json").exists():
        return extract_dir
    for d in Path(extract_dir).iterdir():
        if d.is_dir() and (d / "manifest.json").exists():
            return str(d)
    raise FileNotFoundError("No manifest.json found in zip")


def train(zip_path, epochs_phase1=10, epochs_phase2=10):
    """Run the full training pipeline locally."""

    # ── Unzip ──
    log.info("Unpacking %s...", zip_path)
    tmp_dir = tempfile.mkdtemp(prefix="bird_train_")
    with zipfile.ZipFile(zip_path, "r") as z:
        z.extractall(tmp_dir)

    data_root = find_data_root(tmp_dir)
    manifest = json.loads((Path(data_root) / "manifest.json").read_text())
    train_dir = os.path.join(data_root, "train")
    test_dir = os.path.join(data_root, "test")
    ood_dir = os.path.join(data_root, "ood_test")

    log.info("Export from %s", manifest.get("created", "unknown"))
    log.info("Camera: %s", manifest.get("camera", "unknown"))

    species_list = sorted(os.listdir(train_dir))
    num_species = len(species_list)
    train_count = sum(len(os.listdir(os.path.join(train_dir, s))) for s in species_list)
    test_count = sum(len(os.listdir(os.path.join(test_dir, s)))
                     for s in os.listdir(test_dir) if os.path.isdir(os.path.join(test_dir, s)))

    log.info("%d species, %d train images, %d test images", num_species, train_count, test_count)
    for sp in species_list:
        n = len(os.listdir(os.path.join(train_dir, sp)))
        log.info("  %s: %d", sp.replace("_", " "), n)

    # ── Data generators ──
    train_datagen = tf.keras.preprocessing.image.ImageDataGenerator(
        rescale=1.0 / 255,
        horizontal_flip=True,
        brightness_range=[0.85, 1.15],
        rotation_range=10,
        zoom_range=[0.85, 1.0],
        fill_mode="nearest",
    )
    test_datagen = tf.keras.preprocessing.image.ImageDataGenerator(rescale=1.0 / 255)

    train_data = train_datagen.flow_from_directory(
        train_dir, target_size=(IMG_SIZE, IMG_SIZE),
        batch_size=BATCH_SIZE, class_mode="categorical", shuffle=True, seed=42,
    )
    test_data = test_datagen.flow_from_directory(
        test_dir, target_size=(IMG_SIZE, IMG_SIZE),
        batch_size=BATCH_SIZE, class_mode="categorical", shuffle=False,
    )
    class_names = list(train_data.class_indices.keys())

    # ── Build model ──
    log.info("Building model (MobileNetV2 pretrained on ImageNet)...")
    base_model = tf.keras.applications.MobileNetV2(
        include_top=False, weights="imagenet",
        input_shape=(IMG_SIZE, IMG_SIZE, 3), pooling="avg",
    )
    base_model.trainable = False
    model = tf.keras.Sequential([
        base_model,
        tf.keras.layers.Dropout(0.3),
        tf.keras.layers.Dense(num_species, activation="softmax"),
    ])
    model.compile(
        optimizer=tf.keras.optimizers.Adam(learning_rate=0.01),
        loss="categorical_crossentropy",
        metrics=["accuracy"],
    )

    # ── Phase 1: Train head only ──
    log.info("Phase 1: Training classification head (%d epochs)...", epochs_phase1)
    h1 = model.fit(train_data, epochs=epochs_phase1, validation_data=test_data, verbose=1)
    phase1_acc = h1.history["val_accuracy"][-1]
    log.info("Phase 1 done — val accuracy: %.1f%%", phase1_acc * 100)

    # ── Phase 2: Fine-tune top 20% of layers ──
    log.info("Phase 2: Fine-tuning (unfreezing top 20%% of base model)...")
    base = model.layers[0]
    base.trainable = True
    num_layers = len(base.layers)
    freeze_until = int(num_layers * 0.8)
    for layer in base.layers[:freeze_until]:
        layer.trainable = False
    log.info("  %d of %d layers unfrozen", num_layers - freeze_until, num_layers)
    model.compile(
        optimizer=tf.keras.optimizers.Adam(learning_rate=0.0001),
        loss="categorical_crossentropy",
        metrics=["accuracy"],
    )
    h2 = model.fit(train_data, epochs=epochs_phase2, validation_data=test_data, verbose=1)
    phase2_acc = h2.history["val_accuracy"][-1]
    log.info("Phase 2 done — val accuracy: %.1f%%", phase2_acc * 100)

    # ── Evaluate: In-distribution ──
    log.info("Evaluating on holdout test set...")
    from sklearn.metrics import classification_report
    test_data.reset()
    predictions = model.predict(test_data, verbose=0)
    pred_classes = np.argmax(predictions, axis=1)
    true_classes = test_data.classes
    species_names = [s.replace("_", " ") for s in class_names]
    report = classification_report(true_classes, pred_classes,
                                    target_names=species_names, output_dict=True)
    overall_acc = report["accuracy"]
    log.info("Overall accuracy: %.1f%% %s", overall_acc * 100,
             "PASS" if overall_acc >= 0.80 else "FAIL (need >=80%)")
    print(classification_report(true_classes, pred_classes, target_names=species_names))

    # ── Evaluate: OOD ──
    ood_results = []
    if os.path.exists(ood_dir) and os.listdir(ood_dir):
        log.info("Evaluating out-of-distribution detection...")
        for species_dir in sorted(os.listdir(ood_dir)):
            sp_path = os.path.join(ood_dir, species_dir)
            if not os.path.isdir(sp_path):
                continue
            abstained = 0
            total = 0
            for img_name in os.listdir(sp_path):
                try:
                    img = tf.keras.preprocessing.image.load_img(
                        os.path.join(sp_path, img_name), target_size=(IMG_SIZE, IMG_SIZE))
                    arr = tf.keras.preprocessing.image.img_to_array(img) / 255.0
                    pred = model.predict(np.expand_dims(arr, 0), verbose=0)[0]
                    if float(np.max(pred)) < CONFIDENCE_THRESHOLD:
                        abstained += 1
                    total += 1
                except Exception:
                    continue
            if total > 0:
                rate = abstained / total
                status = "PASS" if rate >= 0.95 else "WARN"
                log.info("  %s %s: %d/%d abstained (%.0f%%)",
                         status, species_dir.replace("_", " "), abstained, total, rate * 100)
                ood_results.append({"species": species_dir, "abstained": abstained,
                                    "total": total, "rate": rate})

        if ood_results:
            overall_ood = sum(r["abstained"] for r in ood_results) / sum(r["total"] for r in ood_results)
            log.info("Overall OOD abstention: %.0f%% %s", overall_ood * 100,
                     "PASS" if overall_ood >= 0.95 else "NEEDS TUNING")

    # ── Quantize ──
    log.info("Quantizing model (full INT8)...")
    saved_model_dir = os.path.join(tmp_dir, "saved_model")
    model.export(saved_model_dir)

    def representative_dataset():
        train_data.reset()
        for i, (images, _) in enumerate(train_data):
            for img in images:
                yield [np.expand_dims(img, 0).astype(np.float32)]
            if i >= 200 // BATCH_SIZE:
                break

    converter = tf.lite.TFLiteConverter.from_saved_model(saved_model_dir)
    converter.optimizations = [tf.lite.Optimize.DEFAULT]
    converter.representative_dataset = representative_dataset
    converter.target_spec.supported_ops = [tf.lite.OpsSet.TFLITE_BUILTINS_INT8]
    converter.inference_input_type = tf.uint8
    converter.inference_output_type = tf.uint8
    tflite_model = converter.convert()

    quant_path = os.path.join(tmp_dir, "yard_model_quant.tflite")
    with open(quant_path, "wb") as f:
        f.write(tflite_model)
    log.info("Quantized model: %s (%.0f KB)", quant_path, len(tflite_model) / 1024)

    # ── Compile for Edge TPU (Docker) ──
    log.info("Compiling for Edge TPU via Docker...")
    edgetpu_path = quant_path.replace(".tflite", "_edgetpu.tflite")
    docker_cli = "/Applications/Docker.app/Contents/Resources/bin/docker"

    import subprocess
    try:
        result = subprocess.run(
            [docker_cli, "run", "--rm",
             "-v", f"{tmp_dir}:/data",
             "ghcr.io/google-coral/edgetpu-compiler:latest",
             "edgetpu_compiler", "/data/yard_model_quant.tflite", "-o", "/data"],
            capture_output=True, text=True, timeout=120,
        )
        if result.returncode != 0:
            log.warning("Docker edgetpu_compiler failed: %s", result.stderr)
            log.warning("Trying alternative Docker image...")
            result = subprocess.run(
                [docker_cli, "run", "--rm",
                 "-v", f"{tmp_dir}:/data",
                 "debian:bullseye-slim", "bash", "-c",
                 "apt-get update -qq && apt-get install -qq -y curl gnupg && "
                 "curl -sL https://packages.cloud.google.com/apt/doc/apt-key.gpg | apt-key add - && "
                 "echo 'deb https://packages.cloud.google.com/apt coral-edgetpu-stable main' > /etc/apt/sources.list.d/coral.list && "
                 "apt-get update -qq && apt-get install -qq -y edgetpu-compiler && "
                 "edgetpu_compiler /data/yard_model_quant.tflite -o /data"],
                capture_output=True, text=True, timeout=300,
            )
        if result.returncode == 0 and os.path.exists(edgetpu_path):
            log.info("Edge TPU model: %s (%.0f KB)",
                     edgetpu_path, os.path.getsize(edgetpu_path) / 1024)
        else:
            log.warning("Edge TPU compilation failed. The quantized model is still usable.")
            log.warning("Stderr: %s", result.stderr[-500:] if result.stderr else "none")
            edgetpu_path = None
    except Exception as e:
        log.warning("Docker not available for Edge TPU compilation: %s", e)
        log.warning("The quantized .tflite model is saved but not compiled for Edge TPU.")
        log.warning("Upload yard_model_quant.tflite to Colab and run edgetpu_compiler there.")
        edgetpu_path = None

    # ── Save outputs ──
    labels = [s.replace("_", " ") for s in class_names]
    labels_path = MODELS_DIR / "yard_model_labels.txt"

    # Backup old model
    yard_model_path = MODELS_DIR / "yard_model.tflite"
    if yard_model_path.exists():
        backup = MODELS_DIR / "yard_model_prev.tflite"
        shutil.copy2(yard_model_path, backup)
        log.info("Backed up previous model to %s", backup)

    # Copy new model
    if edgetpu_path and os.path.exists(edgetpu_path):
        shutil.copy2(edgetpu_path, yard_model_path)
        log.info("Deployed Edge TPU model to %s", yard_model_path)
    else:
        shutil.copy2(quant_path, yard_model_path)
        log.info("Deployed quantized model to %s (not Edge TPU compiled)", yard_model_path)

    # Save labels
    labels_path.write_text("\n".join(labels) + "\n")
    log.info("Labels saved: %s (%d species)", labels_path, len(labels))

    # Training report
    training_report = {
        "trained_at": manifest.get("created", ""),
        "model_type": "MobileNetV2 transfer learning",
        "species": labels,
        "num_species": len(labels),
        "total_training_images": train_count,
        "total_test_images": test_count,
        "phase1_val_accuracy": float(phase1_acc),
        "phase2_val_accuracy": float(phase2_acc),
        "overall_accuracy": float(overall_acc),
        "per_species_accuracy": {
            sp: float(report[sp]["f1-score"]) for sp in species_names if sp in report
        },
        "ood_results": ood_results,
        "confidence_threshold": CONFIDENCE_THRESHOLD,
        "passed_accuracy_gate": overall_acc >= 0.80,
    }
    report_path = MODELS_DIR / "training_report.json"
    report_path.write_text(json.dumps(training_report, indent=2))
    log.info("Report saved: %s", report_path)

    # Cleanup
    shutil.rmtree(tmp_dir, ignore_errors=True)

    log.info("")
    log.info("=" * 50)
    log.info("TRAINING COMPLETE")
    log.info("  Accuracy: %.1f%% %s", overall_acc * 100,
             "PASS" if overall_acc >= 0.80 else "FAIL")
    log.info("  Model: %s", yard_model_path)
    log.info("  Labels: %s", labels_path)
    log.info("")
    log.info("To deploy, restart the classifier:")
    log.info("  launchctl unload ~/Library/LaunchAgents/com.vives.bird-classifier.plist")
    log.info("  launchctl load ~/Library/LaunchAgents/com.vives.bird-classifier.plist")

    return training_report


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    parser = argparse.ArgumentParser(description="Train yard bird classifier locally")
    parser.add_argument("zip_path", help="Path to training_export.zip")
    parser.add_argument("--epochs", type=int, default=10, help="Epochs per phase (default: 10)")
    args = parser.parse_args()

    train(args.zip_path, epochs_phase1=args.epochs, epochs_phase2=args.epochs)


if __name__ == "__main__":
    main()
