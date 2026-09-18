> Analyst output from the 2026-09-17 deep pass (coordinator: Fable 5.1). Evidence files live beside this document in `2026-09-17-flagship-postmortem-evidence/` (referenced below as `evidence/`). Status: findings, not decisions — see `../progress/2026-09-17-deep-pass-report.md` §3 and §9.

# Flagship / yard-model post-mortem — why "the flagship was crap", with evidence

**Analyst:** forensic ML/data pass, 2026-09-17. Read-only on both machines (`sqlite3 "file:…?mode=ro"`, `ssh` to the Pi with no writes). No repo file modified, no model run or trained.
**Predecessor notes:** `EVIDENCE_LOG.md` in this directory (its RESULT blocks were used as data; probes it already ran were not repeated).
**Evidence files:** `evidence/` — `manifest_provenance_join.txt`, `imac_db_probes.txt`, `pi_db_probes.txt`, `contact_train_*.png` (4 sheets), `contact_pi_prefix_exotics_vs_recent.png`, `pi_sample/list.txt`.

Convention: **[M]** = measured today from DB/files; **[E]** = evidenced by an artefact or commit; **[I]** = inferred / only asserted in docs or memory (no artefact); **[U]** = unknowable from the evidence.

David's question, restated: *"we've done it before and the flagship was crap even though I put in all that work so there was some database issues maybe … find out where the weak parts of this system are."*

Short answer: the only trained classifier that ever ran live is the 2026-04-08 "yard model" (12 species, 672 training images, MobileNetV2 transfer learning, 76% on a leaky random split, **deployed although it failed its own 80% gate**). It was crap live for three compounding reasons — tiny/imbalanced training set, closed 12-class output forced onto every visitor, and a confidence path that hid how wrong it was — and then, from 2026-04-25 (RC3), **its own labels became the canonical labels in the iMac database and folders**. As a result, the "Phase 1 DONE" flagship manifest built on 2026-06-29 has **71% of its 114,717 training rows labelled by the failed model**, and AIY disagrees with the label on 43% of all training rows. Contact sheets show ~10–20% label precision in those pools. There was no database *corruption*; the "database issue" is that the schema never recorded **who produced a label** (human vs which model), **which frame/bbox the label refers to**, or **which camera/era**, so nobody could see that the training pool had been poisoned by the model under repair. Nothing called "flagship" has ever been trained (no checkpoints/.pt/.hef/mlruns anywhere).

---

## 1. TIMELINE of every training attempt that can be evidenced

| # | Date | What | Model / backbone | Data (source, size, split) | Reported metric | Artefacts | What happened next | Status |
|---|---|---|---|---|---|---|---|---|
| 0 | 2026-03-15 | YOLOv8n bird **detector** (context only, not the classifier) | YOLOv8n, Colab | `dataset.zip` 574 files, YOLO format | — | `/Users/vives/bird-classifier/dataset.zip`, `train_bird_detector.ipynb`, `Google Collab/` | Became `models/yolov8n_bird.onnx` | [E] |
| 1 | 2026-03-28 | **Yard model attempt 1 — Coral weight imprinting** (commit `f179b1c` "yard model trained — 44 species in 66 seconds") | `pycoral.learn.imprinting.ImprintingEngine` on `mobilenet_v1_1.0_224_l2norm_quant_edgetpu.tflite` (`train_yard_model.py:30`) | Human reviews via `train_yard_model.py:get_training_data()` (correct + wrong-with-correction), `MIN_IMAGES_PER_SPECIES=15` (`:38`). **44 species.** At the time no camera or multi-bird filter (both added later — `project_forget_me_nots.md:30`). No image list/manifest kept. | "0/14 on real birds" | `models/yard_model_broken.tflite` (4.5 MB, Mar 28 00:39), `yard_model_broken_labels.txt` (44 lines); probe log in `probe_yard_model.py:16-73` (scores compressed 3–12, 'not a bird' class useless) | Visual audit found squirrels in Rock Pigeon/Field Sparrow/Cedar Waxwing/Flicker…, empty frames labelled Flicker, Titmice labelled Waxwing → lessons `feedback_verify_data_first.md`, `feedback_camera_training.md`. Model shelved. | Existence [E]; "0/14" [I] (memory only, no metrics file); training image list [U] |
| 2 | 2026-04-08 12:03 | **Attempt 2 — weight imprinting on a "clean" pool** | same imprinting engine | "853 feeder-only, single-bird, confirmed images, 12 species" (`project_retraining_ui.md:11,25`). Today the same SQL filters return 1,009 correct + 222 corrected = 1,231 (reviews grew), so 853 is plausible but not reproducible [M]. | 31% top-1; "everything collapsed into American Goldfinch" | `models/yard_model_prev.tflite` (4.5 MB, mtime Apr 8 12:03; `strings` shows `MobilenetV1/Logits/…` → imprinting model) | David: "fuck this weight imprinting… no shortcuts" → real transfer learning | Existence [E]; 31% [I] (docs/memory only); image list [U] |
| 3 | 2026-04-08 20:58 → 23:00 | **Attempt 3 — MobileNetV2 transfer learning (this is the model that ran live)** commits `5879ffa/4837d44/507380e/3cc046f` | `train_local.py:94-104` MobileNetV2 ImageNet, head 10 ep + fine-tune top 20% 10 ep, INT8 PTQ (`:184-207`), edgetpu_compiler (`:210-249`) | `train_export.py` → `~/docs/bird-observatory/training-exports/training_export_20260408_205832.zip` manifest.json: **12 species, train 672 / test 175 / ood 21**, feeder-only, single-bird, `min_images=15`, **random stratified 80/20, seed 42** (`train_export.py:166-194`). Per-class train: Song Sparrow 18, Cardinal 38, Cowbird 40, Titmouse 41, Junco 42, House Finch 44, WB Nuthatch 54, Goldfinch 57, Wren 59, Hairy 66, Chickadee 84, Downy 129; test 5–33 per class. | `models/training_report.json`: phase1 val 0.789, phase2 0.760, overall **0.76**; Junco 0.51, Titmouse 0.67, Hairy 0.69, Song Sparrow 1.00 (n=5); **`passed_accuracy_gate: false`** (gate ≥0.80, `train_local.py:147`) | `models/yard_model.tflite` (2.9 MB, Apr 8 23:00; `strings` → `serving_default_keras_tensor_155:0`, `StatefulPartitionedCall_1:0` = Keras export, **no** MobilenetV1 strings), `yard_model_labels.txt` (12) | **Deployed anyway**: `train_local.py:251-268` backs up the previous model to `_prev` and copies the new one to `yard_model.tflite` unconditionally. No evidence the visual hard gate was applied to this export [U]. | [E] |
| 3a | 2026-04-10/11 | Yard model wired into the live v3 pipeline (`fc53ef8`, `c34c29b`, `8a3b4a8`) | — | — | — | `pipeline/classifier.py:95-127`: feeder path yard-first; accept if conf ≥ `confident_threshold` (0.25 per `28-yard-model-training.md:35-37`), AIY only if yard < 0.10 | Ran live on the iMac from Apr 10 **to today** (Sep 2026 rows: 1,249/1,267 have `model_source='yard'` [M]) | [E][M] |
| 3b | 2026-04-17/18 | "Yard is always 100% confident" fix | — | — | — | `yard_classifier.py:225-270`: divides the uint8 output by T=100 then softmaxes; max reachable confidence ≈ 0.538 | The comment misidentifies the model as weight-imprinted; the "[255, 29, 4, 0…]" outputs are the **INT8-quantized softmax** of the Keras model (255 = 1.0), i.e. the model's own probability was discarded and replaced by an arbitrary temperature | [E] |
| 3c | 2026-04-25 | **RC3 step 1: "preserve lock-time vote info as canonical, store auth as metadata"** (`00dd8bc`) | — | — | — | iMac `pipeline/snapshot_writer.py:406-413,464,499-507`; spec `2026-04-25-imac-live-classify-as-built.md:200` (which still says the opposite: "DB labels are AIY's") | From this commit the iMac `common_name` (= folder the image is filed under) is the **yard model's** label; AIY's opinion only in `extra_json.authoritative`. [M]: May–Sep 2026 `common_name == lock_time.species` for 100% of rows; AIY disagrees on 64% (May), 71% (Jun), 90% (Jul), 94% (Aug), 88% (Sep) | [E][M] |
| 4 | 2026-04-23 | **Tier 2 Phase 0 — eval harness + AIY baseline** (`87a8332`, `40966fc`, `f9ac394`) | n/a (scoring only) | 1,670 human reviews (correct/wrong/trash) SQL-joined (`tier2_eval/baseline.py:92-106`) | AIY: top-1 0.680, macro-F1 0.752, ECE 0.163; Downy recall 0.41, Hairy precision 0.43, Blue Jay precision 0.39, `not_a_bird` recall 0.0 (`baseline.report.json`) | `tier2_eval/{baseline,metrics,split}.py`, `baseline.report.json` | Plan v1 + 4 lit-reviews (`~/docs/bird-observatory/working/specs/2026-04-23-*.md`) | [E] |
| 4a | 2026-04-26 | "RC2 from calibration" plan: 263 verdicts in 4 buckets → "~76% yard precision", `training_label` field | — | — | "263 verdicts" | `tools/calibration-results-20260426T040751Z.csv` has **13 data rows** (malformed columns); `working/plans/2026-04-26-rc2-from-calibration.md:6,11-15` | `training_label` **never shipped** (0 hits in pipeline/, classifications_db.py, api.py) | 263 [I]; 76% [U] |
| 4b | 2026-04-29 | Tier 2 Phase 1 tooling (`e7424a9`, `f57317b`) + readiness checkpoint | cleanlab | "34K weak AIY labels, captured 2025-11-15, at `~/bird-classifier/data/bird_crops_train_labeled/`" marked **✅ Ready** (`tier2-readiness-checkpoint.md:23,90`) | — | `tools/tier2_phase1_cleanlab.py:38`, `tools/verify_training_data.py:23` | **The directory does not exist on the iMac or the Pi** (checked `find`/`mdfind` 2026-09-17). The cleanlab script feeds **dummy one-hot pred_probs** (`:87-91,112-114`) so it would find 0 issues. Never run (no `label_issues.csv` anywhere). | [M] |
| 5 | 2026-06-29 | **Flagship dossier + "Phase 1 data prep DONE"** (`94c660e`, `ffafc47`) | EfficientNet-Lite0 planned | `tools/build_flagship_manifest.py` → `~/bird-snapshots/flagship/manifest.csv`: 119,899 rows; train 114,717 / val 3,864 / test 1,279 / ood_test 39; 15 species + not_a_bird (34 negatives) | dossier claims "leakage check 0", "100% bbox coverage", train = "weak **AIY** labels" | manifest.csv (16.6 MB); `2026-06-29-flagship-classifier-design.md:66-67` | **Phase 2 never started** (blocked on GPU). [M] join to DB: **train rows labelled by yard = 81,176/114,717 (71%)**; yard-labelled *and* AIY disagrees = 49,894 (43%); ground-camera rows 12,257; era window rows 123. | [E][M] |
| 5a | 2026-06-29 | Post-hoc AIY calibration on the Pi (`2203a21`) | isotonic map | fit on **all 1,875 iMac reviews** (`tools/fit_calibration.py:17-24`; `pipeline/calibration.py:9-17`) | CV ECE 0.182 → 0.057 | `pipeline/calibration.py` | Same reviews are the only hold-out any future model can be scored on → calibration/eval leakage baked in | [E] |
| — | 2026-04-25 → 07-01 | Pi ✓/✗ review collection | — | 828 verdicts (yes 638 / no 190), all `model_source='aiy_onnx'`, **`correct_species=''` on all 828** [M] | — | `~/bird-snapshots/logs/pi_reviews.db` | Stopped 2026-07-01; review port 07-07 added history (4 rows, all undone); ✗→species picker shipped **today** (`23aff61`) | [M] |

**Bottom line for the timeline:** three yard-model training runs (Mar 28, Apr 8 ×2) and zero flagship training runs. The word "flagship" in David's question refers to the Apr 8 MobileNetV2 model, which the docs mislabel as "Attempt 2 … weight-imprinted" (`28-yard-model-training.md:5,374`) — the artefacts say otherwise.

---

## 2. RANKED ROOT CAUSES of "the flagship was crap"

### RC1 — The training pool is labelled by the model being replaced, and since 2026-04-25 by the *failed* model. Confidence: **HIGH [M]**
- Every label in `~/bird-snapshots/classified/` is a model output; human-verified labels are 1,875/173,405 iMac rows (1.1%) and 828/64,877 Pi rows (1.3%) [M].
- RC3 (`pipeline/snapshot_writer.py:406-413`, commit `00dd8bc`) made the **lock-time vote canonical**. On the iMac the lock-time source is the 12-class yard model (`pipeline/classifier.py:95-104`). [M] by month (`evidence/imac_db_probes.txt`): 2026-05 51,258 rows, 50,237 yard-locked, AIY disagrees 32,979; 2026-07 27,373 rows, disagrees 24,582 (90%); 2026-08 17,329, disagrees 16,308 (94%).
- The 2026-06-29 flagship manifest (`tools/build_flagship_manifest.py:60-75` takes the label from the **folder name**) therefore inherits it: **train 81,176/114,717 yard-labelled (71%); 49,894 with AIY disagreeing (43%)**. Per class (train+val): Carolina Wren 95% yard / 83% disagree; White-breasted Nuthatch 90% / 78%; American Goldfinch 89% / 66%; Dark-eyed Junco 77% / 74%; Downy 78% / 49%; Hairy 75% / 49% (`evidence/manifest_provenance_join.txt`). The dossier calls this pool "weak AIY labels" (`2026-06-29-flagship-classifier-design.md:28,51`) — it is not.
- Visual check (`evidence/contact_train_*.png`, 16 random train crops each with bbox): **American Goldfinch ≈ 2/16 correct** (rest: chickadee, titmouse, Cardinal in IR, House Finch, Chipping Sparrow, 2 empty feeder crops); **Carolina Wren ≈ 1–2/16**; **Dark-eyed Junco 3/16 — all three are pre-v3 AIY-labelled March frames, 0/13 of the yard-labelled ones are Juncos** (they are House Finches, a House Sparrow, a chickadee, two empty frames); Mourning Dove 16/16 correct (AIY-labelled, ground camera).
- Mechanism: a 12-class closed-set model (`models/yard_model_labels.txt`) must answer one of 12 names for every crop, including the ~53% of disagreements where AIY's own confidence is < 0.05 (49,896 of 94,294 [M]) — i.e. crops that are probably empty or unidentifiable. Those names became folders, and folders became "training data".

### RC2 — The model that went live was trained on 672 images, failed its gate, and was deployed anyway; its "76%" was measured on a leaky random split. Confidence: **HIGH [E][M]**
- `training_export_20260408_205832.zip/manifest.json`: 672 train / 175 test; **6 of 12 classes had < 50 training images** (Song Sparrow 18 … House Finch 44); test n = 5–33 per class, so per-class numbers like "Song Sparrow 1.00" are noise.
- Split = `random.Random(42).shuffle` per species (`train_export.py:181-190`). Reviewed frames are bursty: of 1,875 usable reviews, **1,180 (63%) share the same camera+minute with another reviewed frame; 1,875 reviews fall into 555 five-minute buckets (≈3.4 per bucket)** [M]. Near-duplicate frames straddled train/test → the 0.76 is inflated by an unknown amount. `tier2_eval/split.py` (visit-grouped) was written 15 days later and never used by any training script.
- `train_local.py:147` says FAIL below 0.80; `training_report.json:40` `passed_accuracy_gate: false`; `train_local.py:256-268` deploys regardless. `28-yard-model-training.md:21` then reports "~76% per the 2026-04-26 calibration verdicts (263 samples)" — the CSV has 13 rows.

### RC3 — Live behaviour was judged through a confidence path that could not express "I don't know", so a closed-set model looked decisive while hallucinating. Confidence: **HIGH [E][M]**
- Quantized softmax output (uint8, 255 = 1.0) was treated as logits and re-softmaxed with T=100 (`yard_classifier.py:263-270`) → confidence ∈ [0.08, 0.54]; the accept gate was 0.25 (`28-yard-model-training.md:35`); AIY only consulted when yard < 0.10 (`pipeline/classifier.py:107`). A top-1 quantized probability above ~0.55 is enough to accept the yard label without AIY.
- 12 output classes, no `not_a_bird`, no `unknown`: every squirrel, empty box, and non-listed species becomes one of 12 names (`project_yard_model_revamp.md:11`; the 2026-04-25 snapshot audit's smoking gun: empty feeder structure → "American Goldfinch 1%", `docs/superpowers/progress/historical/2026-04-25-detection-snapshot-audit-findings.md`).
- Net effect David saw: "everything is Goldfinch" / hallucinated species on the live overlay. It was never scored on a fair held-out set after deployment; the baseline harness scores **AIY**, not yard (`tier2_eval/baseline.py:18-22` — yard scoring "stubbed out … not activated").

### RC4 — Snapshot/crop alignment eras put empty or wrong boxes into the corpus. Confidence: **HIGH (Pi) [M]; MEDIUM (iMac residue) [M]**
- **Pi, before 2026-05-12** (hi-res ring buffer matched the wrong frame; fixed by `34ae15a`, `7f3b21b`, `92dd6a2`, `787810d`): **30,144 of 64,877 classified rows (46%)** are pre-fix. Labels that exist only pre-fix: Carolina Chickadee 1,074, Baltimore Oriole 680, Great-tailed Grackle 519, "finch" 318 (a folder literally named `finch`, all from 2026-04-25 13:14–13:15), Rock Pigeon 155, Eurasian Magpie 137, Rose-breasted Grosbeak 128 [M]. `evidence/contact_pi_prefix_exotics_vs_recent.png`: all five "Baltimore Oriole" boxes sit on the **orange/yellow feeder cup at the base of the pole** (a stationary object, bbox ≈ [1210, 840, 1350, 990] across four different days); "Great-tailed Grackle" is an empty feeder corner; "Carolina Chickadee" is a real Black-capped Chickadee (regional filter never applied: `range_filter_applied=0` on all 64,877 Pi rows [M]). The coordinator's measurement stands: 175/190 Pi 'no' verdicts are pre-fix artefacts.
- **iMac, 2026-04-19 21:16 → 04-23** (Sonoma hi-res re-crop stale bbox, `tools/cull_hallucination_window.py:1-12,39`): 11,293 rows culled, but reviewed rows were preserved — **276 rows still `action='classified'`** in the window, 33 'correct' / 42 'wrong' reviews inside it, 123 of them in the flagship train split and 30 in its test split [M].
- Independent of eras, the yard-labelled iMac pool contains empty boxes (contact sheets: 2/16 Goldfinch, 2/16 Junco, 1/16 Wren crops are empty feeder). Box size: train sample p10 width 80 px, **21% of train boxes have a side < 80 px** (`evidence`: 84/400) [M].

### RC5 — Evaluation flaws: nothing that was called "accuracy" would have caught RC1–RC4. Confidence: **HIGH [E][M]**
- (a) Leaky random split (RC2). (b) Live "crap" never measured (RC3). (c) "263 verdicts" unverifiable. (d) The flagship manifest's **test set = 'correct' verdicts only** (`build_flagship_manifest.py:143-154`: `wrong` → `continue`): it drops all **231 human corrections** including the **100 Hairy→Downy** ones (`reviews.correct_species='Downy Woodpecker'` n=100 [M]) — the #1 confusion pair cannot be measured on that test set; `reclassify` rows are also unusable because **all 138 have empty `correct_species`** [M] (the dossier counts them as "corrected label", `:29-31`). (e) The Pi calibration map was fit on the same 1,875 reviews (`tools/fit_calibration.py:24`). (f) Resolution domain shift: manifest train sample 72% 640×360 vs test 85% 1920×1080 [M]; bbox p50 width 157 px train vs 366 px test. iMac corpus is 100% 1080p in March, 100% 640×360 from May on; Pi mixes both within the same month and is ~100% 640×360 since August (the HLS hi-res path is mostly missing now) [M]. (g) The test set contains **182 ground-camera rows** and the val split is a `hash()`-ordered sample with admitted within-visit leakage (`build_flagship_manifest.py:174-176`).

### RC6 — Ground vs feeder camera mixing. Confidence: **MEDIUM-HIGH [M]**
- Attempt 1 mixed cameras (squirrels; `feedback_camera_training.md`). Attempts 2–3 were feeder-only [E].
- The flagship manifest has **no camera column** (`:194`). Mourning Dove: train 6,612 ground / 215 feeder, test 60/60 ground; Red-bellied Woodpecker test 52/62 ground; Blue Jay test 23/32 ground; Song Sparrow test 63/83 from the no-prefix March era [M]. A model trained on it learns "grass + 1080p = Mourning Dove". 12,257 train rows are ground camera; the live classifier runs only on the feeder camera on both machines.

### RC7 — Class imbalance / unlearnable classes. Confidence: **MEDIUM [M]** (real, but secondary to RC1–RC2)
- Apr 8: 6/12 classes < 50 train images (see RC2). Flagship manifest: House Finch 34,712 vs Red-bellied Woodpecker 40 (868×), Blue Jay 197, Hairy 1,355; not_a_bird 34. Under RC1 the big classes are also the dirtiest (House Finch 77% yard-labelled), so imbalance amplifies the noise.

### RC8 — Schema gaps that made this invisible (and still make a trustworthy dataset impossible today). Confidence: **HIGH [M]**
- `classifications` (identical on both machines, `classifications_db.py:117-125` iMac / `:157-191` Pi): no `label_source`, `track_id`, `pts`, `model_source`, `lock_species`/`auth_species`, `image_w/h`, `bbox_space` columns — they live only inside `extra_json` (absent on the 23,483 pre-v3 iMac rows; present on 149,922 iMac / 64,877 Pi rows). `bbox` = `best_detection_json.box` in the saved image's pixel space, which is 640×360 or 1920×1080 depending on whether the HLS fetch succeeded (`pipeline/snapshot_writer.py:463-479`) — consistent today (0/800 sampled boxes exceed the image) but not enforced.
- `reviews.reviewer` = 'dashboard' on all 2,285 rows (`reviews_db.py:92`); `review_history` covers only 454 of them (since 04-24). `bird_index` set on 65 rows while 110 'correct' verdicts are on multi-bird frames — which bird was confirmed is ambiguous.
- Pi `pi_reviews`: `correct_species` never captured (0/190 'no') until today; no `reviewer`. `pipeline_tracks.best_keeper_path` NULL on 10,121/10,121 rows; tracker `track_id` resets per pipeline restart (track 48 spans 2026-09-10 → 09-17 in `pipeline_events`) and `pipeline_tracks.track_id` is a different AUTOINCREMENT space (390,711) → **no join from a snapshot to its track**; events retained ~7 days (`event_store.py:229`).
- Folder hygiene: dual `Space Dir`/`Underscored_Dir` for 12+ species (107 dirs on iMac, 344 on Pi), a Pi folder named `finch`, 4,992 iMac files with no camera prefix. `reclassify` verdicts carry no species (138) and `apply_verdict` treats them like `correct` (`dashboard/api.py:462`).
- No dataset manifest standard: Apr 8 `manifest.json` = counts only; Jun 29 CSV = `path,label,split,source,bbox` (no camera, timestamp, model_source, image size, verdict id, reviewer). Neither attempt 1 nor 2 left any image list at all.

### RC9 — Unknowability is itself a finding. Confidence: **HIGH**
- We cannot reconstruct what Attempts 1 and 2 were trained on (no manifests) [U]; the "0/14" and "31%" numbers exist only in memory/docs [I]; the "263-verdict 76%" has a 13-row CSV [U]; the docs (`28-yard-model-training.md:5,17,374`, `yard_classifier.py:230`) say the deployed model is weight-imprinted MobileNet V1 while `training_report.json` and the tflite strings say Keras MobileNetV2 — a doc "audit correction" corrected the wrong way [M]; the readiness checkpoint marks a 34K-image dataset "Ready" that does not exist [M]. When the artefacts and the story disagree this often, the process — not just the data — is the weak part.

---

## 3. DATA-PIPELINE MUST-FIX LIST (ordered; each must be true before any training)

1. **Quarantine yard-era labels; record label provenance as a column.**
   Where: `pipeline/snapshot_writer.py` (Pi `:546-608`, iMac `:406-507`) writes `model_source`, `track_id`, `pts`, `lock_time`, `authoritative`, `disagreement` into `extra_json` via `classifications_db.py` `_KNOWN_FIELDS` (`iMac :117-125`, Pi `:157-165`). Promote to real columns: `label_source` (aiy_onnx | aiy | yard | aiy_batch | human), `lock_species`, `auth_species`, `auth_confidence`, `disagreement`, `track_id`, `pts`, `model_source`; backfill from `extra_json`; set `label_source='aiy_batch'` for the 23,483 pre-v3 iMac rows. Then: **no row with `label_source='yard'` may enter a training pool**, ever.
   Acceptance: `SELECT count(*) FROM classifications WHERE action='classified' AND (label_source IS NULL OR track_id IS NULL OR pts IS NULL)` = 0 for rows written after the change; backfill covers 149,922 iMac and 64,877 Pi rows; the manifest builder prints per-split counts by `label_source` and refuses to run if any train row is `yard`.

2. **Capture `correct_species` on ✗ and make it canonical — verify today's change, then harden it.**
   Verified [E]: `dashboard/pi_dash.html:1424-1430` routes a ✗ click to `openPicker()`; `:1302-1314 pickOption()` → `kbVerdict(file,'no',name)` posts `{verdict:'no', correct_species}`; `dashboard/pi_review.py:355` reads it and `:371-380` persists it to `pi_review_history` **and** `pi_reviews.correct_species`. So yes, it is persisted (both tables, `~/bird-snapshots/logs/pi_reviews.db`).
   Gaps: species pool is a 14-name list (`pi_dash.html:1205-1210`) plus whatever the feed has shown; free text is accepted with title-casing only (`:1309-1311`); the server does no validation (`pi_review.py:355`); `pi_reviews` has no `reviewer`. Fix: validate server-side against the canonical common-name list (`models/inat_bird_labels.txt` ∪ `models/chilmark_feeder_species.txt`), 400 on unknown names (or store as `unknown_text` for later mapping); add `reviewer`; add `bbox_confirmed` (0/1) so a ✗ can say "wrong box" vs "wrong species".
   Acceptance: after 20 real ✗ verdicts, `SELECT count(*) FROM pi_reviews WHERE verdict='no' AND correct_species=''` = 0 and every `correct_species` is in the canonical list. The 190 historical 'no' rows stay species-less (unusable for confusion analysis).

3. **Era / crop-validity flags in the DB, applied by every dataset builder.**
   Pi: `source_timestamp < '2026-05-12'` → `crop_valid=0` (30,144 rows; the stored bbox does not describe the stored image). iMac: `'2026-04-19T21:16' ≤ source_timestamp < '2026-04-23T12:00'` → `crop_valid=0` (276 rows). Add `no_bird` (from today's `pipeline/pi_classifier.py:48-57` signal) and `image_w`, `image_h`, `bbox_space` columns so a crop can be validated without opening the file.
   Acceptance: manifest builder excludes `crop_valid=0`; `SELECT count(*) FROM manifest WHERE crop_valid=0` = 0; 0 rows where bbox exceeds `image_w/h`.

4. **Visual verification gate script (contact sheets) — mandatory, per class × label_source × camera.**
   Prototype = what produced `evidence/contact_train_*.png` (bbox crop + 15% pad, date, image size, box size, label source, AIY second opinion). Ship as `tools/contact_sheet.py`: sample N=24 per class per `label_source` per camera from the *proposed* train manifest; David marks each tile right/wrong; script writes `gate.json` with per-class sampled precision.
   Acceptance: every class in train has sampled precision ≥ 0.95 (n ≥ 24) or is excluded; `gate.json` is committed next to the manifest and its sha1 is embedded in the manifest header. Today's sampled precision: Goldfinch ~0.13, Carolina Wren ~0.1, Junco (yard-labelled) 0.0, Mourning Dove 1.0 — the gate would fail.

5. **A dataset manifest format that carries provenance.**
   Replace `build_flagship_manifest.py:194` columns with: `path, sha1, label, label_source, verdict_id, reviewer, camera, source_timestamp, pts, track_id, visit_id, x1,y1,x2,y2, image_w, image_h, crop_valid, era, split, split_policy, gate_sha1`. Builder changes: `:60-75` stop taking the label from the folder name for non-human rows — take it from the DB row's `auth_species` when `label_source` is AIY, never from `yard`; `:137-154` include `wrong` rows with `correct_species` as test truth; `:157` filter by camera (feeder-only for the feeder model).
   Acceptance: the manifest round-trips (every row's sha1 matches the file); `label_source` distribution printed; 0 `yard` rows in train/val; 0 ground rows in a feeder model's train/test.

6. **Dedup-by-visit split policy + chronological hold-out.**
   Use `tier2_eval/split.py:derive_visit_ids` (300 s, per camera; exists, unused) and `assert_no_visit_leakage` (`:112-127`) in the manifest builder instead of the `hash()` sample (`build_flagship_manifest.py:174-176`); keep the last 4 weeks of reviewed data as a chronological test.
   Acceptance: `assert_no_visit_leakage` passes for train∩val and train∩test; no test frame within 300 s of a train frame on the same camera; test is ≥ 60% from a period after the newest train frame.

7. **Eval harness on human-verified labels only, per class, both machines.**
   Extend `tier2_eval/baseline.py:92-106` (already uses correct/wrong/trash) to (a) read Pi `pi_reviews` (yes / no+correct_species / not_a_bird), (b) filter `crop_valid=1`, (c) report per-class precision/recall with bootstrap CI (`metrics.py` has it), per camera, plus the explicit confusion cells Hairy↔Downy and House Finch↔American Goldfinch, (d) fit any calibration (`tools/fit_calibration.py:24`) on a disjoint review fold.
   Acceptance: `python -m tier2_eval.baseline --human-only` runs on both DBs and prints n per class; classes with n < 35 are reported as "insufficient", not as a percentage.

8. **Folder / verdict hygiene.**
   Merge dual `Space`/`Underscore_` dirs (iMac 107 dirs, Pi 344); rename or remove the Pi `finch` folder (318 rows) and other pre-fix exotic labels once `crop_valid=0` is set; make `reviews.reviewer` real; either record `bird_index` for multi-bird 'correct' verdicts or exclude frames with `json_array_length(birds_json)>1` from truth (110 today); give `reclassify` a meaning or stop offering it (`dashboard/api.py:371,462`).
   Acceptance: one dir per species on each machine; `SELECT count(*) FROM reviews WHERE verdict='reclassify' AND correct_species=''` stops growing; multi-bird truth rows carry a `bird_index`.

9. **Delete or fix dead tooling and false "Ready" claims.**
   `tools/verify_training_data.py:23` and `tools/tier2_phase1_cleanlab.py:38` point at a directory that does not exist on either machine; the cleanlab script uses dummy one-hot `pred_probs` (`:87-91,112-114`) and would prune nothing. `docs/working/progress/tier2-readiness-checkpoint.md:23,90` marks the 34K set "✅ Ready".
   Acceptance: both scripts either take the new manifest as input and run end-to-end on it, or are removed; checkpoint doc corrected.

10. **Reconcile docs with artefacts.**
    `28-yard-model-training.md:5,17,374` and `yard_classifier.py:230` (weight-imprinted claim) vs `models/training_report.json:3` + tflite strings (Keras MobileNetV2); `2026-04-25-imac-live-classify-as-built.md:200` ("DB labels are AIY's") vs RC3 code; dossier `:28-31` ("weak AIY labels", "138 reclassify corrected") vs measurements above.
    Acceptance: a `MODEL_PROVENANCE.md` per model file (training date, script+commit, manifest sha1, metrics file) exists for every `.tflite/.onnx/.hef` in `models/`.

---

## 4. WHAT DAVID SHOULD DO (his review time is the scarce resource)

**How many verdicts per class for a trustworthy eval set.** For a per-class precision/recall estimate at 95% confidence around p≈0.9: ±10 pp needs n≈35 verdicts of that class; ±5 pp needs n≈140 (normal approximation, 1.96²·p(1−p)/e²). Post-fix Pi 'yes' today: House Finch 88, Northern Cardinal 50, White-breasted Nuthatch 45, Downy 20, American Goldfinch 19, Tufted Titmouse 19, Black-capped Chickadee 14, House Sparrow 11, **Hairy Woodpecker 0** (6 'no', species unknown). Getting every core feeder species to n=35 is ≈150 more verdicts (~15 min at 5 s each); to n=140 is ≈900 (~75 min). That is the whole budget for a first honest eval set — far less than the effort already spent.

**Priorities, in order.**
1. **Hairy vs Downy**: 0 verified Hairy post-fix on the Pi; iMac baseline Downy recall 0.41 / Hairy precision 0.43; 100 Hairy→Downy corrections exist on the iMac but are excluded from the flagship test set. Every ✗ on a woodpecker must name the species (now possible).
2. **House Finch vs American Goldfinch** (females/juveniles): Goldfinch n=19 post-fix; the yard-labelled Goldfinch pool is ~13% precise.
3. Chickadee, Titmouse, Nuthatch, Cardinal to n≥35.
4. A **corpus audit, not just a queue**: 24 tiles per class from the yard-labelled iMac pool and from pre-fix Pi 'yes' rows (372) to decide whether any of it is salvageable as full-frame (bbox-less) data. Expect to discard most of the yard-labelled pool.

**UI asks (ordered by verdict-per-minute payoff).**
- Queue sampling that targets under-reviewed classes and low-margin votes (Hairy/Downy first) instead of the "recent" strip.
- Show the exact crop the model saw (bbox drawn) and let ✗ say "wrong box" vs "wrong species".
- "Which bird?" click for multi-bird frames (`bird_index`).
- A reviewer identity and a per-class progress meter toward n=35 / n=140.
- Keep the keyboard flow; add "same as previous" for bursts (3.4 reviewed frames per 5-minute visit today — bursts are the norm).

---

## 5. INVENTORY of usable human-verified data TODAY (per species) [M]

iMac = `reviews` joined to `classifications` (correct + wrong→species), all eras; "iMac post-fix" = source_timestamp ≥ 2026-04-23 12:00 (after the cheap-restore); "iMac window" = 2026-04-19 21:16 → 04-23 (alignment artefacts); reviewing ended 2026-05-09. Pi = `pi_reviews` 'yes' by snapshot era (pre-fix bbox unreliable); Pi 'no' post-fix have no species. iMac reviews are 84% feeder (1,136 vs 213 ground 'correct').

| Species | iMac verified total (feeder/ground) | iMac in window | iMac post-fix | Pi yes pre-05-12 (bbox unreliable) | Pi yes post-05-12 | Pi no post-05-12 (species unknown) |
|---|---|---|---|---|---|---|
| Black-capped Chickadee | 241 (240/1) | 4 | 128 | 13 | 14 | 0 |
| House Finch | 221 (220/1) | 28 | 121 | 213 | 88 | 0 |
| Downy Woodpecker | 179 (179/0) | 0 | 10 | 4 | 20 | 0 |
| Carolina Wren | 92 (92/0) | 0 | 5 | 6 | 0 | 0 |
| Hairy Woodpecker | 89 (89/0) | 0 | 0 | 0 | **0** | 6 |
| Song Sparrow | 86 (73/13) | 0 | 0 | 1 | 0 | 0 |
| American Goldfinch | 83 (83/0) | 0 | 0 | 0 | 19 | 0 |
| Tufted Titmouse | 77 (76/1) | 3 | 10 | 7 | 19 | 2 |
| Brown-headed Cowbird | 75 (66/9) | 0 | 0 | 0 | 0 | 0 |
| Dark-eyed Junco | 74 (55/19) | 0 | 1 | 0 | 0 | 0 |
| White-breasted Nuthatch | 71 (71/0) | 0 | 0 | 1 | 45 | 1 |
| Northern Cardinal | 68 (62/6) | 0 | 3 | 0 | 50 | 1 |
| Red-bellied Woodpecker | 62 (10/52) | 0 | 0 | 0 | 0 | 0 |
| Mourning Dove | 61 (0/61) | 0 | 0 | 0 | 0 | 2 |
| Blue Jay | 41 (10/31) | 0 | 0 | 0 | 0 | 0 |
| Red-winged Blackbird | 20 (0/20) | 0 | 0 | 0 | 0 | 1 |
| Chipping Sparrow | 16 (16/0) | 3 | 13 | 25 | 0 | 0 |
| Pine Warbler | 13 (13/0) | 0 | 0 | 0 | 0 | 0 |
| House Sparrow | 1 (1/0) | 0 | 1 | 1 | 11 | 0 |
| Baltimore Oriole | 0 | 0 | 0 | 90 (stationary-object FP suspected, see RC4) | 0 | 0 |
| Others (Starling 4, W-crowned Sparrow 3, Robin 2, W-throated Sparrow 1; Pi: Bullock's Oriole 6, Carolina Chickadee 2, Mockingbird 1, Spotted Towhee 1, Altamira Oriole 1) | 10 | 0 | 0 | 11 | 0 | 2 |
| **Totals** | **1,580** (feeder 1,362 / ground 218) | 38 | 292 | 372 | **266** | 15 |

Also available: iMac `wrong→not_a_bird` 157 + `trash` 339 (trash images are deleted on disk — dossier `:66a`), so **no clean not_a_bird test images exist**; repo `dataset_negatives/` has 34 images. Corpus (unverified, model-labelled): iMac 173,707 files (152,492 `feeder_`, 16,223 `ground_`, 4,992 no-prefix; ≈108k+ of them yard-labelled since 04-25); Pi 64,877 rows (30,144 pre-fix / 34,733 post-fix), 17 GB.

Caveats on the table: iMac "verified" rows from the yard era (217 'correct' + 117 'wrong' where the reviewed label came from yard) are still valid human truth for the *saved image*; the 110 'correct' verdicts on multi-bird frames are ambiguous about which bird; Pi pre-fix 'yes' rows confirm the label matches the saved frame but the stored bbox does not.

---

## SUMMARY

**Top 3 root causes**
1. Self-labelling turned into self-poisoning: since RC3 (2026-04-25) the iMac's canonical label is the failed 12-class yard model's guess; 71% of the 2026-06-29 flagship train manifest (81,176/114,717 rows) is yard-labelled, 43% with AIY disagreeing; contact sheets show ~0–20% label precision in those pools (`evidence/contact_train_*.png`, `manifest_provenance_join.txt`).
2. The only model that ever ran live was trained on 672 images (6/12 classes < 50), scored 0.76 on a leaky random split (63% of reviewed frames share a minute with another), failed its own 0.80 gate and was deployed anyway (`train_local.py:147,256-268`; `training_report.json:40`); live it ran as a closed 12-class set behind a double-softmax confidence that could not say "unknown".
3. The schema never recorded provenance (who/which model labelled, which frame/bbox/track, which camera/era): `label_source`, `track_id`, `pts`, `model_source` only in `extra_json`; `reviewer` always 'dashboard'; Pi `correct_species` never captured (0/190) until today; Pi pre-05-12 rows (46% of its corpus) have bboxes that don't describe the saved image — so nobody could see the poisoning or measure the Hairy/Downy confusion.

**Top 5 must-fixes (before any training):** (1) provenance columns + backfill, and a hard rule that `label_source='yard'` never enters a train pool; (2) validated `correct_species` on every ✗ (shipped today in `pi_dash.html:1424-1430` → `pi_review.py:355-380`; add server validation + reviewer); (3) `crop_valid`/era + image-size/bbox-space columns applied by every builder; (4) a per-class contact-sheet gate (`gate.json`, ≥0.95 sampled precision) embedded in a provenance-carrying manifest format; (5) visit-grouped + chronological splits via `tier2_eval/split.py` and a human-only per-class eval that includes the 231 `wrong→species` corrections and Pi verdicts.

**Data available now:** 1,580 iMac human-verified species labels (feeder 1,362 / ground 218; ended 2026-05-09) + 266 post-fix Pi 'yes' (8 species; Hairy Woodpecker 0) + 372 pre-fix Pi 'yes' usable only as full-frame labels; no clean not_a_bird images; the "34K weak labels" dataset does not exist; nothing named "flagship" has ever been trained.
