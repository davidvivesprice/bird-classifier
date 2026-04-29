# Tier 2 Flagship Training — Readiness Checkpoint (2026-04-29)

**Status:** Phase 0 (eval harness) ✅ shipped. Phases 1–8 (training pipeline) awaiting Phase 0 → Phase 1 kickoff signal.

## Current Baseline (AIY, held-out split of 1,670 human-verified reviews)

| Metric | Value | Target | Gap |
|---|---|---|---|
| Top-1 Accuracy | 67.96% | ≥75% | +7 pp |
| Macro-F1 | 75.22% | ≥65% | ✓ exceeded |
| ECE (Calibration) | 16.3% | ≤5% | -11.3 pp |
| OOD detection | (not measured yet) | ≥0.85 AUROC | TBD |

**Baseline captured:** `tier2_eval/baseline.report.json` (last updated 2026-04-23). Current pipeline is running on Pi 5 with AIY ONNX CPU classifier — this baseline is the measurement point.

---

## Phases 1–8 Training Pipeline (Per `tier2-training-plan-v1.md`)

### Phase 1: Cleanlab Label Quality (Highest ROI step)
**Status:** Ready to run. No blocking dependencies.

- Input: 34K weak AIY labels from `~/bird-classifier/data/bird_crops_train_labeled/`
- Operation: `cleanlab.find_label_issues()` on the weak label set
- Expected output: ~10–30% pruned (typical for weak → clean transition)
- Environment: Offline, overnight-able on iMac or Colab
- **Next action:** Run cleanlab, capture `label_issues.csv` and pruned dataset stats

### Phase 2: Representation Learning (balanced instance sampling)
**Status:** Pending Phase 1 output. Data & hyperparams ready.

- Input: 34K weak labels (post-cleanlab pruned)
- Backbone: **EfficientNet-Lite0** (unanimous choice across 4 lit-reviews)
- Loss: Balanced instance sampling (Kang 2020)
- Hardware: iMac GPU or Colab (training on x86, inference compile for Pi/Hailo later)
- **Deliverable:** `efficientnet_lite0_backbone.pt`

### Phase 3: Head Training (class-balanced, logit adjustment)
**Status:** Pending Phase 2. Loss functions ready.

- Input: Backbone from Phase 2
- Loss: **Logit Adjustment** (Menon 2021, NOT focal loss) on class-balanced batches
- Classes: 14 species + `not_a_bird` + `unknown` (16 total)
- **Deliverable:** `efficientnet_lite0_head.pt`

### Phase 4: Specialist Heads (Hairy/Downy, etc.)
**Status:** Pending Phase 3. Known misclassification list exists.

- High-confusion pairs: Hairy/Downy Woodpecker, House Finch/Purple Finch, etc.
- Per Kang 2020 + Foret 2020 (SharpDarts): low-rank specialist adapters after shared backbone
- **Deliverable:** `specialist_heads/` directory with per-pair adapters

### Phase 5: OOD Gate (Energy-based, Outlier Exposure)
**Status:** Pending Phase 4. OOD dataset ready (`374 non-bird samples + 22K culled JPGs`).

- Energy-based anomaly detection (Liu 2020)
- Outlier Exposure (OE): calibrate on non-bird + hard negatives
- **Target:** ≥0.85 AUROC on OOD detection
- **Deliverable:** `ood_gate.pt` (binary OOD classifier)

### Phase 6: Quantization Awareness Training (QAT)
**Status:** Pending Phase 5. PTQ baseline ready.

- Input: Full trained model from Phase 5
- QAT for INT8 (Hailo target) + INT16 (legacy Coral fallback if needed)
- **Deliverable:** `flagship_int8.hef` (via Hailo DFC on x86_64) + `flagship_int16.tflite`

### Phase 7: Shadow Deployment (≥7 days parallel)
**Status:** Pending Phase 6 (model file). Harness ready.

- Parallel running: flagship vs AIY on same frame stream
- Validation: Per-track agreement ratio, per-species ROC curves
- Success gate: ≥0.90 intra-frame agreement, no regressions on rare species
- **Deliverable:** `shadow_validation_report.json` (per-species metrics)

### Phase 8: Live Cutover + Monitoring
**Status:** Pending Phase 7 validation.

- Swap `flagship_pending` registry entry with trained model
- Live hybrid inference: flagship (16 classes) + AIY fallthrough (`unknown` tail catch)
- Monitoring: Per-track confidence histograms, species-level precision tracking
- **Deliverable:** Live system, monitoring instrumentation in place

---

## Data & Resource Readiness Checklist

| Item | Status | Notes |
|---|---|---|
| 34K weak AIY labels | ✅ Ready | Captured 2025-11-15; stored at `~/bird-classifier/data/bird_crops_train_labeled/` |
| 1,670 human-verified hold-out | ✅ Ready | Balanced by visit (temporal split); used by `tier2_eval` harness |
| 374 non-bird OOD samples | ✅ Ready | For Outlier Exposure calibration |
| 22K culled JPGs (hard negatives) | ✅ Ready | Discarded by AIY; useful as OOD hard cases |
| Training plan (all phases) | ✅ Ready | `~/bird-classifier/docs/superpowers/specs/2026-04-23-tier2-training-plan-v1.md` |
| Lit-review (4 papers) | ✅ Ready | `2026-04-23-litreview-*.md` — LSTM, Kang, Menon, Liu, Foret, Outlier Exposure |
| Evaluation harness | ✅ Ready | `tier2_eval/` — 26 green tests, generates per-species metrics |
| Hailo DFC compiler | ⚠️ x86-only | Runs on iMac or Linux box; produces .hef for Pi |
| Hailo compilation docs | ✅ Ready | `2026-04-23-litreview-4-quantization-deployment.md` |

**Data audit gate:** Per `feedback_verify_data_first.md` (hard lesson from yard-0/14 disaster), **visually sample ≥5 crops per species before Phase 2 training starts**. This is a blocking gate — failure to do this caused the prior yard model to produce 0/14 accuracy.

---

## Blocking Dependencies & Handoff Points

| Gate | Blocking | Unlocked by | Action |
|---|---|---|---|
| Phase 1 → 2 | Cleanlab output | Phase 1 completion | Use pruned label set for Phase 2 |
| Phase 2 → 3 | Backbone training | Phase 2 completion | Head training starts on frozen backbone |
| Phase 3 → 4 | Class-balanced head | Phase 3 completion | Specialist heads train on frozen shared model |
| Phase 4 → 5 | Specialist adapters | Phase 4 completion | OOD gate training on full expert model |
| Phase 5 → 6 | OOD calibration | Phase 5 completion | Hailo DFC compile (x86-only, can run in parallel) |
| Phase 6 → 7 | Quantized model | Hailo DFC output | Shadow deploy harness runs daily, ≥7 days validation |
| Phase 7 → 8 | Shadow validation | Phase 7 completion | Live cutover + monitoring wiring |

---

## Estimated Timeline (Planning)

- **Phase 1 (cleanlab):** 1–2 hours (offline, overnight)
- **Phase 2 (backbone):** 4–6 hours (GPU, typically overnight on iMac)
- **Phase 3 (head):** 2–4 hours (GPU, overnight)
- **Phase 4 (specialists):** 1–2 hours (GPU, typically < 1 hour per adapter)
- **Phase 5 (OOD):** 2–3 hours (GPU, overnight)
- **Phase 6 (QAT + compile):** 4–6 hours (QAT on GPU + Hailo DFC on x86_64)
- **Phase 7 (shadow):** ≥7 days (parallel, no user action after launch)
- **Phase 8 (cutover):** 2–4 hours (swap model, monitor, tune hyperparams if needed)

**Total calendar time:** ~2–3 weeks (phases 1–6 concurrent where possible, then 7-day shadow, then Phase 8).

---

## Next Action (Awaiting David Signal)

Per memory `project_classifier_state.md`: David's "aiy reimplement to finish" likely refers to this Tier 2 pipeline. Before starting Phase 1:

1. **Ask David:** "Tier 2 flagship training — ready to kickoff Phase 1 (cleanlab on 34K weak labels, ~2–3 week timeline)?"
2. **Confirm label set:** Visually audit ≥5 crops per species (THE hard gate from yard-0/14 disaster).
3. **Pick Phase 1 environment:** Offline iMac overnight, or Colab with data upload?
4. **Start cleanlab:** Launch Phase 1 → Phase 2 pipeline.

---

**Checkpoint author:** Pi-Claude, 2026-04-29
**Harness status:** Evaluation working; awaiting phase kickoff signal
**Baseline:** AIY 67.96% / 75.2% / 16.3% (hold-out 1,670 reviews)
**Target:** Flagship ≥75% / ≥65% / ≤5% (+ ≥0.85 OOD AUROC)
