# Audio Detection Accuracy — Design Spec

## Problem

Our audio detection system (audio_analyzer.py) uses the same BirdNET V2.4 model as BirdNET-Go, but misses species that BirdNET-Go catches — notably Northern Cardinal (27 vs 46 single-camera) and Tufted Titmouse (9 vs 30). The root cause is our post-processing pipeline: deep detection requires 2 hits in 15 seconds, a 10-second cooldown suppresses re-detections, and noisereduce may be filtering out faint calls.

The goal is Merlin-quality accuracy: catch every real bird, minimize false positives, and build the foundation for real-time feedback and clip verification (future phases).

### Current Pipeline (what's wrong)

```
RTSP audio → decode 48kHz mono
    → 6s buffer, advance 3s (50% overlap at buffer level)
    → bandpass 300-15kHz + noisereduce + RMS normalization
    → BirdNET inference (min_conf=0.25, overlap=2.0 within 6s)
    → per-slice dedup
    → dynamic threshold check (base 0.50, floor 0.25)
    → deep detection accumulator (2 hits in 15s OR instant at 0.65)
    → cooldown (10s after accept)
    → range filter
    → save to DB + clip
```

**Problems identified:**
1. Deep detection (2 hits in 15s) misses infrequent callers — a bird calling once every 20s at 0.49 is invisible
2. 10s cooldown suppresses active singers — loses 1-2 detections per cycle
3. noisereduce spectral gating (prop_decrease=0.85) may suppress faint calls that overlap with noise floor
4. Only one camera mic — misses birds far from ground cam
5. Dynamic threshold floor (0.25) is higher than BirdNET-Go's (0.20)

### BirdNET-Go's Approach (what works)

BirdNET-Go (when overlap > 0) uses overlap-based confirmation:
- 3s windows analyzed at 1s steps (overlap=2.0 → 3 analyses of same audio)
- Pending accumulator collects detections within a flush window
- Requires N-of-M confirmations — real birds appear consistently, noise doesn't
- No cooldown — every detection counts
- Only EQ preprocessing (highpass + lowpass), no spectral gating

### Merlin Bird ID (the north star)

Same BirdNET model, but delivers:
- Real-time identification with live spectrogram
- Instant feedback — species shown as they sing
- Clip playback — tap any detection to hear it and compare reference
- Accuracy that feels like magic on a phone mic

This spec covers **accuracy only**. Real-time UI and clip playback are future phases.

## Design

### 1. Overlap-Based Confirmation (replaces deep detection)

Replace `DetectionAccumulator` with overlap-based confirmation matching BirdNET-Go's proven approach.

**New analysis flow:**
```
RTSP audio → decode 48kHz mono
    → 3s chunks, sliding 1s steps (overlap=2.0)
    → preprocessing (configurable per run)
    → BirdNET inference per chunk
    → pending accumulator (per-species, 6s flush window)
    → require min_confirmations across overlapping windows
    → dynamic threshold check
    → range filter
    → save to DB + clip
```

**How it works:**
1. Audio arrives as continuous PCM stream
2. Every 1 second, a new 3-second chunk is ready (overlapping with previous by 2s)
3. Run BirdNET on each chunk — returns species + confidence for all detections above 0.25
4. For each detected species, add to `PendingDetection` keyed by species name
5. `PendingDetection` tracks: count of confirmations, highest confidence, best chunk timestamp
6. Flush deadline: 6 seconds after first detection of that species
7. At flush: if count >= `min_confirmations` → accept; otherwise → discard
8. Accepted detections go through dynamic threshold + range filter → save

**Confirmation levels:**
- Level 0 (off): min_confirmations=1 — accept everything above threshold (debugging only)
- Level 1 (lenient): min_confirmations=2 — good for rare/quiet species (default)
- Level 2 (balanced): min_confirmations=3 — stricter, fewer false positives
- Level 3 (strict): 50% of max possible detections in window

Default: Level 1 (lenient). The range filter already eliminates impossible species, so we can afford to be lenient on confirmation.

**What this replaces:**
- `DetectionAccumulator` class — removed entirely
- `DEEP_DETECTION_*` constants — removed
- `DEEP_DETECTION_COOLDOWN` — removed (no cooldown needed; the flush window handles timing naturally)

**Why this is better:**
- Same audio analyzed from 3 time offsets — statistically stronger than "see it twice in 15s"
- No cooldown gaps — every detection in every window counts
- Flush window (6s) is much shorter than old deep detection window (15s) — more responsive
- Matches BirdNET-Go's battle-tested approach

### 2. Multi-Camera Audio

Add a second camera mic for spatial diversity. Ground cam hears birds near the feeder area; magnolia cam hears birds in a different part of the yard.

**Architecture:**
```
ground cam   ──→ RTSPStreamManager("ground")   ──→ analysis thread 1 ─┐
magnolia cam ──→ RTSPStreamManager("magnolia") ──→ analysis thread 2 ─┤──→ birdnet_local.db
                                                                       │
                                                  cross-cam dedup ◄────┘
```

- BirdNET model loaded once, shared across threads (TFLite interpreter is thread-safe for inference)
- Each detection tagged with `source` column in `notes` table
- Cross-camera deduplication: if same species detected within ±10s on different cameras, the detection is flagged `multi_source=true` — higher confidence, definitely a real bird
- Start with ground + magnolia; newbackyard can be added later via config

**RTSPStreamManager integration:**
- Each camera gets its own manager instance with appropriate preferred/fallback streams
- Ground: preferred=ground, fallback=magnolia
- Magnolia: preferred=magnolia, fallback=ground
- Both benefit from the resilience layer built earlier

**Thread safety:**
- Each thread has its own PCM buffer, pending accumulator, and preprocessing state
- Database writes use the existing `_db_lock` mutex
- Clip saving uses camera-specific subdirectories

### 3. Preprocessing A/B Testing

Run two inference passes per audio chunk for one week to find which preprocessing helps and which hurts, per species.

**Two paths:**
1. **Raw+EQ** — bandpass filter only (300Hz-15kHz), no noisereduce, no RMS normalization. Matches what BirdNET was trained on.
2. **Full** — current pipeline (bandpass + noisereduce + RMS). Our custom preprocessing.

**Logging:**
- Each detection saved with `preprocessing` column: "raw" or "enhanced"
- After one week, SQL query compares per-species detection counts and average confidence
- Species where raw catches more → noisereduce is hurting (e.g., faint Cardinal calls)
- Species where enhanced catches more → noisereduce is helping (e.g., Song Sparrow in wind)

**Implementation:**
- Both paths share the same overlap confirmation pipeline
- The second inference pass adds ~100ms per chunk (model is fast)
- Dual-path is temporary — after analysis, we pick the winner or make it species-adaptive
- Can be toggled via environment variable: `AUDIO_AB_TEST=true`

### 4. Threshold Tuning

| Parameter | Current | New | Rationale |
|-----------|---------|-----|-----------|
| `DYNAMIC_THRESHOLD_MIN` | 0.25 | 0.20 | Match BirdNET-Go; catch borderline Cardinals/Titmice |
| `DEEP_DETECTION_*` | enabled | removed | Replaced by overlap confirmation |
| `DEEP_DETECTION_COOLDOWN` | 10s | removed | Overlap confirmation handles timing |
| `MIN_CONFIDENCE` | 0.50 | 0.50 | Keep as-is; dynamic threshold handles species-specific lowering |
| `DYNAMIC_THRESHOLD_TRIGGER` | 0.80 | 0.80 | Keep; lower than BirdNET-Go's 0.90, we learn faster |

### 5. Database Schema Changes

Add columns to `birdnet_local.db` `notes` table:

```sql
ALTER TABLE notes ADD COLUMN source TEXT DEFAULT 'ground';
ALTER TABLE notes ADD COLUMN multi_source INTEGER DEFAULT 0;
ALTER TABLE notes ADD COLUMN preprocessing TEXT DEFAULT 'raw';
ALTER TABLE notes ADD COLUMN confirmations INTEGER DEFAULT 1;
```

- `source`: camera name (ground, magnolia, newbackyard)
- `multi_source`: 1 if same species detected on another camera within ±10s
- `preprocessing`: "raw" or "enhanced" (for A/B testing)
- `confirmations`: how many overlapping windows confirmed this detection

Index: `CREATE INDEX idx_notes_source ON notes(source);`

## Files Changed

| File | Change |
|------|--------|
| `audio_analyzer.py` | Replace deep detection with overlap confirmation; add multi-camera threading; add A/B preprocessing; tune thresholds |
| `birdnet_local.db` | Add source, multi_source, preprocessing, confirmations columns |
| `tests/test_audio_analyzer.py` | New tests for overlap confirmation, multi-camera dedup |

## What This Does NOT Cover

- Real-time UI (live spectrogram, "what's singing now" panel) — future phase
- Clip playback and verification UI — future phase
- Audio-visual fusion (using audio to improve visual classification) — future phase
- BirdNET model upgrade (we're on V2.4, latest available) — N/A
- Enhanced audio stream changes (playback-only, not analysis) — no change needed

## Success Criteria

After one week of running on ground + magnolia:
1. Per-camera detection count matches or exceeds BirdNET-Go on same camera
2. Northern Cardinal and Tufted Titmouse detection counts within 20% of BirdNET-Go
3. False positive rate (checked by spot-checking clips) stays below 10%
4. Species count per day matches or exceeds BirdNET-Go
5. A/B testing data shows clear per-species preprocessing preference
