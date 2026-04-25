# Detection + snapshot audit — findings as we go

**Started:** 2026-04-25 ~11:35 ET, after David surfaced "bbox around background with no bird" on the most recent American Goldfinch snapshot, then "no shot with more than one bird in it is seeing more than one bird."

**Skill in use:** `superpowers:systematic-debugging`. Phase 1 (evidence-gathering) is the entire activity here — no fixes proposed until the picture is complete.

This doc is append-only as evidence comes in.

---

## Smoking gun (the snapshot that started this)

`feeder_2026-04-25_11-26-50_5698.jpg`. Visual inspection:

- **Real content:** chickadee on the lower-left of the feeder, wings spread (taking off)
- **Bbox brackets:** drawn in upper-right around empty feeder structure
- **Label:** "American Goldfinch 1%"
- **DB row:** id 755739, bbox `[439,170,627,327]`, det_conf 0.775, raw_score 1, model_source `aiy`, track_id 5698, vote_history_len 3

The chickadee's location wasn't ignored — it got its own row 0.6s later:
- id 755740, file `feeder_2026-04-25_11-26-50_5699.jpg`, bbox `[27,191,119,356]` (left side, where the chickadee actually is), labeled **"Common Grackle"**, raw_score 2

So in that single frame: TWO separate rows, both with bboxes vaguely in the right region for two distinct things, neither correctly identifying anything, both with raw_score < 5 (~noise).

## Three independent failures stacked

### Failure 1 — YOLO false-positive on empty feeder structure

YOLOv8n detected something at `[439,170,627,327]` with confidence 0.775. There is no bird there in the underlying frame. The feeder body's structural lines (vertical posts of seed columns) likely look enough like a bird silhouette to trigger.

**Boundary tested:** `best_detection_json.confidence` = 0.775 (high) but the visible region is empty. YOLO is seeing patterns that aren't birds.

### Failure 2 — Classifier hallucinates label on background crop

When AIY is given a crop of empty feeder structure, the softmax forces it to pick SOMETHING. raw_score = 1 = essentially noise activation, but top-1 is "American Goldfinch" because the model has to commit. Same dynamic gave the chickadee crop "Common Grackle" with raw_score 2.

This is fundamental softmax behavior — there's no built-in "I don't know" output. Need an explicit confidence floor or OOD gate at the snapshot-write boundary.

### Failure 3 — `authoritative_classify` overwrites lock-time votes with write-time noise

Every snapshot triggers `aiy_relabel` (4844 / 4844 = 100% per the SnapshotWriter stats). The flow:

1. Pipeline votes on frames T1, T2, T3 → vote-lock fires (e.g., yard says Goldfinch with conf 0.5, three votes agree, lock condition met)
2. SnapshotWriter dequeues at write-time T+N
3. Calls `_authoritative_species(p["frame"], p["bbox"])` → re-runs AIY on the lock-time frame at the lock-time bbox
4. **Overwrites `p["species"]`, `p["species_confidence"]`, `p["model_source"]` with the AIY result** (regardless of how confident the lock-time votes were)

Consequence: the saved row's species + raw_score reflect AIY-at-write-time. If the bird left between detection and write (or AIY just struggles with this crop), the saved row shows garbage even when the lock decision was sound.

**This explains why we can't tell from a saved row whether:**
- (a) The lock was sound, AIY just disagrees about the species
- (b) The whole detection was a false positive (no bird ever)
- (c) Bird was real at lock-time, gone by write-time

We lose the lock-time vote-history information at the moment of writing.

## Pattern: how prevalent is the noise?

**raw_score histogram, last 200 classifications:**

| bucket | count | % |
|---|---|---|
| 0-4 (noise) | 37 | 18.5% |
| 5-9 (noise) | 22 | 11.0% |
| 10-24 (low) | 34 | 17.0% |
| 25-49 (moderate) | 13 | 6.5% |
| 50-99 (decent) | 26 | 13.0% |
| 100+ (good) | 68 | 34.0% |

**~30% of saved rows are raw_score < 10** — essentially classifier noise. **~46.5% are raw_score < 25** — barely better than noise.

If we feed these to cleanlab as ground-truth labels for the 34K AIY-labeled training pool, ~30% of our "labels" are the model picking something at random. cleanlab can detect some of this but it's a much harder problem than starting from cleaner labels.

## Multi-bird in pipeline vs in review

**Pipeline DOES capture multi-bird scenes** (per pipeline_events from event_store):

| frame_time | n_tracks | species in frame |
|---|---|---|
| 1777130810001 | 2 | White-breasted Nuthatch \| Tufted Titmouse |
| 1777130447xxx (8 events) | 2 | Song Sparrow \| Dark-eyed Junco |

**SnapshotWriter DOES write per-track** (multiple rows in the same second):

| timestamp | rows | species |
|---|---|---|
| 11:26:50 | 2 | American Goldfinch \| Common Grackle |
| 11:18:51 | 2 | Brown-headed Cowbird \| Red Crossbill |
| 11:11:33 | 2 | Black-capped Chickadee \| White-breasted Nuthatch |

So David's "no shot with more than one bird in it is seeing more than one bird" is about REVIEW UX, not pipeline capture:

- The pipeline writes N rows per multi-bird frame, each with its own bbox
- The annotated JPG for each row only draws **one** set of brackets (the track that snapshot belongs to)
- The reviewer sees N apparently-unrelated single-bird snapshots — no visual cue that they share a frame
- The other birds in the same frame are visible in the JPG content but unmarked

So multi-bird scenes generate N separate review items, each with garbage labels (because Failures 1+2+3 above), and no way to group/cross-reference them during review.

## SnapshotWriter health snapshot (post-restart)

```
submitted: 4844, written: 4844, dropped_full: 0, errors: 0
hires_ok: 0, hires_fail: 0, hires_skipped: 4844  (cheap-restore mode, expected)
aiy_relabel: 4844, aiy_none: 0                   (every snapshot relabeled)
ring_pick_ok: 0, ring_pick_empty: 4844           (no ring on iMac, expected)
```

100% of snapshots are getting AIY relabeled (Failure 3). Zero errors / zero drops — the pipeline is working as DESIGNED, the design just has these three failure modes.

## Detector health (concerning side-finding)

```
yolo_ms_avg: 212ms, yolo_ms_p99: 542ms
detections_total: 275200
```

iMac YOLO is at 212ms avg, 542ms p99. Doc says expected ~98ms with CoreML. Either CoreML isn't being used, the iMac CPU is under heavy load, or both. Worth a separate investigation — could be a contributor to other latency / multi-bird issues. Logging in side-findings.

---

## Root-cause hypotheses (ordered by impact on data quality)

| # | Root cause | Evidence | Fix shape |
|---|---|---|---|
| RC1 | YOLO produces false positives on empty feeder structure | bbox at [439,170,627,327] on empty area; det_conf 0.775 (confident-wrong) | Either retrain YOLO with hard-negatives or add a downstream filter (motion-gate-style: skip detections that don't move across frames) |
| RC2 | AIY hallucinates species on out-of-distribution crops | raw_score 1 on chickadee-crop labeled "Common Grackle"; raw_score 1 on empty-feeder-crop labeled "American Goldfinch"; ~30% of saved rows have raw_score < 10 | Confidence floor at write boundary OR OOD gate. Don't write rows where authoritative-classify is below threshold; route to a separate "uncertain" queue. |
| RC3 | `authoritative_classify` overwrites lock-time vote info with write-time noise | 100% aiy_relabel rate; saved row's species/conf reflects write-time AIY only; lock-time evidence is lost | Preserve both: save the lock-time vote winner AND the authoritative result; flag disagreement; let reviewer see both. |
| RC4 | Annotated JPG only marks one bbox in multi-bird frames | Pipeline + DB capture multi-bird (N rows per frame); JPG annotation has only the snapshot's track | Annotation should draw all active-track bboxes with the snapshot's track highlighted distinctly. Review UI should also surface "this snapshot belongs to a multi-bird frame, see also rows X, Y." |

## RC3 watershed — 2026-04-25 14:06:38 ET (commits 00dd8bc + 7f1634b + 2bb9a55)

iMac `bird-pipeline` restarted at ~14:06:30 ET to load Task-1 + Task-2 of the
RC3 plan. First post-watershed classifications row = **id 756294** (`source_timestamp 2026-04-25T14:06:38.886018`, `House Finch`).

**Pre-watershed rows (id ≤ 756293):** opaque noise. Saved species/confidence
reflect write-time AIY only; lock-time vote info is lost.

**Post-watershed rows (id ≥ 756294):** every row carries:
- `extra_json.lock_time.{species,confidence,source}` — the live pipeline's
  vote-lock decision (canonical "what the system thought" record)
- `extra_json.authoritative.{species,confidence,source}` — write-time AIY
  second opinion (metadata, not canonical)
- `extra_json.disagreement` — bool, true iff lock-time and authoritative
  disagree on species

**First 7-row sample disagreement rate: 14.3%** (1 disagreement out of 7).
That one disagreement is the textbook noise case — yard locked
"Dark-eyed Junco" at conf 0.488, AIY at write time says "House Finch" at
conf 0.04. Both can't be right; AIY's confidence is essentially noise; the
crop probably doesn't cleanly contain the bird the yard model thought it saw.

**SQL filter to identify suspect rows going forward:**
```sql
SELECT * FROM classifications
WHERE id >= 756294 AND action='classified'
  AND json_extract(extra_json,'$.disagreement') = 1
  AND json_extract(extra_json,'$.authoritative.confidence') < 0.1;
```

This is the foundation for RC2 (confidence floor at write boundary, will
likely surface these same rows automatically) and any cleanlab work.

**Use commit hash `2bb9a55` as the cutoff** for any cleanup or
training-pool filtering — pre-2bb9a55 rows have no provenance.

## What I'm NOT proposing yet

No fixes. Per systematic-debugging Phase 1, we keep gathering evidence until the picture is complete enough to make ONE focused fix at a time. This doc is the evidence record; future entries will track fix attempts + verification.

---
