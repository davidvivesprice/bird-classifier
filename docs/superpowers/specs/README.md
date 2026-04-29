# Specs

Active reference documents describing how the system works right now.

## Active specs

| File | What it covers |
|------|----------------|
| `2026-04-25-imac-live-classify-as-built.md` | **Start here.** Complete data flow, clock alignment, snapshot path, adaptive lock math, every constant's location. The authoritative reference for the live detection subsystem. |
| `2026-04-25-review-ui-helpers.md` | Shared JS helpers in `dashboard/index.html` — `applyVerdictToUI`, `loadQueue`, pagination, multibird tri-state semantics. For anyone touching the Review tab. |
| `2026-04-23-airtight-review-system.md` | Review system design + shipped state — `review_history` table, `review2` API, keyset pagination, idempotency. What was built vs. what was deferred. |
| `2026-04-23-tier2-training-plan-v1.md` | Tier 2 yard model training plan — EfficientNet-Lite0, Cleanlab, visit-grouped splits, augmentation. Partially superseded by 2026-04-26 calibration (see caveat at top of doc). |
| `2026-04-23-litreview-1-bird-classifiers.md` | Literature review: architecture and augmentation for fine-grained bird classification. |
| `2026-04-23-litreview-2-calibration-ood.md` | Literature review: uncertainty quantification and out-of-distribution detection. |
| `2026-04-23-litreview-3-small-noisy-imbalanced.md` | Literature review: training on small, noisy, imbalanced datasets. |
| `2026-04-23-litreview-4-quantization-deployment.md` | Literature review: quantization and deployment on Coral Edge TPU and Hailo 8L. |

## `historical/`

Design documents for features that have been implemented and superseded, abandoned, or whose system context no longer exists (e.g. NAS architecture, v1/v2 pipeline designs). Kept for context on how decisions were made.
