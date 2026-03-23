#!/usr/bin/env python3
"""Real-time bird audio analyzer using BirdNET V2.4 via birdnetlib.

Pulls audio from an RTSP camera stream via FFmpeg, analyzes 3-second chunks
with BirdNET, saves detected clips, and writes to a local SQLite database.
The dashboard API (api.py) reads this DB for the "In the Yard" panel and
species charts.

Designed to replace BirdNET-Go with a simpler, faster, fully-controlled solution.

Usage:
    python3 audio_analyzer.py          # run continuously
    python3 audio_analyzer.py --test   # analyze one chunk and exit
"""

import contextlib
import datetime
import io
import json
import logging
import os
import signal
import sqlite3
import sys
import threading
import time
import wave
from pathlib import Path

import numpy as np
from scipy.signal import butter, sosfilt

from metrics import MetricsRegistry
from overlap_confirmation import OverlapConfirmation
from rtsp_stream import RTSPStreamManager
_metrics = MetricsRegistry()

# ── Configuration ──────────────────────────────────────────────────────────
LAT = float(os.environ.get("BIRDNET_LAT", "41.35"))
LON = float(os.environ.get("BIRDNET_LON", "-70.73"))
MIN_CONFIDENCE = float(os.environ.get("BIRDNET_MIN_CONF", "0.50"))

SAMPLE_RATE = 48000
CHANNELS = 1
CHUNK_SECONDS = 3
CHUNK_BYTES = SAMPLE_RATE * 2 * CHANNELS * CHUNK_SECONDS  # 288,000 bytes

ANALYSIS_SECONDS = 3   # feed 3 seconds at a time (single BirdNET window)
ANALYSIS_BYTES = SAMPLE_RATE * 2 * CHANNELS * ANALYSIS_SECONDS
ADVANCE_SECONDS = 1    # slide by 1 second (2s overlap between consecutive chunks)
ADVANCE_BYTES = SAMPLE_RATE * 2 * CHANNELS * ADVANCE_SECONDS

# ── Dynamic Thresholding (from BirdNET-Go) ────────────────────────────────
# When a species is detected at high confidence, lower the threshold for it
DYNAMIC_THRESHOLD_ENABLED = True
DYNAMIC_THRESHOLD_TRIGGER = 0.80   # confidence that triggers threshold lowering
DYNAMIC_THRESHOLD_MIN = 0.20       # lowest a dynamic threshold can go
DYNAMIC_THRESHOLD_HOURS = 24       # how long lowered thresholds last

# ── Overlap Confirmation ─────────────────────────────────────────────────
OVERLAP_CONFIRMATION_ENABLED = True
OVERLAP_FLUSH_WINDOW = 6.0     # seconds to accumulate before deciding
OVERLAP_MIN_CONFIRMATIONS = 2  # overlapping windows needed to accept (level 1)

DB_PATH = Path(
    os.environ.get(
        "BIRDNET_DB_PATH",
        os.path.expanduser("~/bird-snapshots/birdnet-audio/birdnet_local.db"),
    )
)
CLIPS_DIR = Path(
    os.environ.get(
        "BIRDNET_CLIPS_DIR",
        os.path.expanduser("~/bird-snapshots/birdnet-audio/clips"),
    )
)
CLIP_MAX_AGE_DAYS = 30  # auto-delete clips older than this

# ── Nighttime Pause ──────────────────────────────────────────────────────
# No birds call in the dark. Skip inference from sunset+30min to sunrise
# to save CPU. Uses the same NOAA solar algorithm as classify.py.
NIGHT_OFFSET_MINUTES = 30  # minutes after sunset to keep analyzing

from solar_utils import solar_times, is_nighttime

# ── Audio Preprocessing ───────────────────────────────────────────────────
# Two-stage noise reduction pipeline + RMS normalization:
#   1. Bandpass filter (300Hz–15kHz) — removes sub-bass rumble and ultrasonic noise
#   2. noisereduce spectral gating — suppresses broadband noise within the passband
#   3. RMS normalization — restores original signal level so the model sees audio
#      at the amplitude it was trained on

# Stage 1: Bandpass filter
BANDPASS_LOW = 300
BANDPASS_HIGH = 15000
BANDPASS_ORDER = 4
_bandpass_sos = butter(BANDPASS_ORDER, [BANDPASS_LOW, BANDPASS_HIGH],
                       btype='band', fs=SAMPLE_RATE, output='sos')

# Stage 2: noisereduce spectral gating
NOISE_REDUCE_ENABLED = True
try:
    import noisereduce as nr
except ImportError:
    nr = None
    NOISE_REDUCE_ENABLED = False


def preprocess_audio(audio_float32):
    """Apply bandpass + spectral noise reduction + RMS normalization.

    Pipeline: bandpass → noisereduce spectral gating → RMS match.
    The final RMS normalization restores the original signal level so the model
    receives audio at the amplitude it was trained on, with noise replaced by
    silence rather than the overall volume being crushed.

    Args:
        audio_float32: numpy float32 array, values in [-1, 1]

    Returns:
        Cleaned float32 audio array, same shape, matched to original RMS.
    """
    # Measure original RMS before any processing
    original_rms = np.sqrt(np.mean(audio_float32 ** 2))

    # Stage 1: Bandpass filter (sub-millisecond)
    cleaned = sosfilt(_bandpass_sos, audio_float32).astype(np.float32)

    # Stage 2: Spectral noise reduction (~250-500ms for 6s@48kHz)
    if NOISE_REDUCE_ENABLED and nr is not None:
        cleaned = nr.reduce_noise(
            y=cleaned, sr=SAMPLE_RATE,
            stationary=True,
            n_fft=1024,
            prop_decrease=0.85,
        ).astype(np.float32)

    # Stage 3: RMS normalization — rescale so cleaned audio matches original level.
    # This ensures the model sees the same amplitude it was trained on.
    # The noise is gone but the bird call signal fills the original dynamic range.
    cleaned_rms = np.sqrt(np.mean(cleaned ** 2))
    if cleaned_rms > 1e-10:  # avoid division by zero on silence
        cleaned = cleaned * (original_rms / cleaned_rms)
        # Clip to [-1, 1] to prevent rare edge-case clipping
        np.clip(cleaned, -1.0, 1.0, out=cleaned)

    return cleaned

# ── Logging ────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("audio_analyzer")


# ── Globals ────────────────────────────────────────────────────────────────
_shutdown = threading.Event()


# ── Dynamic Threshold Manager ─────────────────────────────────────────────
class DynamicThreshold:
    """Lowers detection threshold for species recently seen at high confidence.

    Mirrors BirdNET-Go's dynamic threshold system:
    - 1st high-conf detection: threshold → 75% of base
    - 2nd: 50% of base
    - 3rd+: 25% of base (clamped to DYNAMIC_THRESHOLD_MIN)
    """

    def __init__(self):
        self._species = {}  # species_name -> {"count": int, "last_seen": float}

    def _expire(self):
        """Remove entries older than DYNAMIC_THRESHOLD_HOURS."""
        cutoff = time.time() - DYNAMIC_THRESHOLD_HOURS * 3600
        expired = [k for k, v in self._species.items() if v["last_seen"] < cutoff]
        for k in expired:
            del self._species[k]

    def record_detection(self, species_name, confidence):
        """Record a high-confidence detection to lower future thresholds."""
        if confidence < DYNAMIC_THRESHOLD_TRIGGER:
            return
        entry = self._species.get(species_name)
        if entry:
            entry["count"] += 1
            entry["last_seen"] = time.time()
        else:
            self._species[species_name] = {"count": 1, "last_seen": time.time()}

    def get_threshold(self, species_name):
        """Get the effective confidence threshold for a species."""
        self._expire()
        entry = self._species.get(species_name)
        if not entry:
            return MIN_CONFIDENCE

        count = entry["count"]
        if count >= 3:
            factor = 0.65
        elif count == 2:
            factor = 0.75
        else:
            factor = 0.85

        return max(MIN_CONFIDENCE * factor, DYNAMIC_THRESHOLD_MIN)

    def should_accept(self, species_name, confidence):
        """Check if a detection meets the (possibly lowered) threshold."""
        threshold = self.get_threshold(species_name)
        return confidence >= threshold


# ── Database ───────────────────────────────────────────────────────────────
_db_conn = None
_db_lock = threading.Lock()


def init_db():
    """Create the notes table if it doesn't exist. Opens persistent connection."""
    global _db_conn
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    _db_conn = sqlite3.connect(str(DB_PATH), timeout=10, check_same_thread=False)
    _db_conn.execute("PRAGMA journal_mode=WAL")
    _db_conn.execute(
        """
        CREATE TABLE IF NOT EXISTS notes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_node TEXT DEFAULT '',
            date TEXT NOT NULL,
            time TEXT NOT NULL,
            common_name TEXT NOT NULL,
            scientific_name TEXT NOT NULL,
            confidence REAL NOT NULL,
            clip_name TEXT DEFAULT '',
            input_file TEXT DEFAULT ''
        )
        """
    )
    _db_conn.commit()

    # Add columns for multi-camera and A/B testing (idempotent)
    for col_sql in [
        "ALTER TABLE notes ADD COLUMN source TEXT DEFAULT 'ground'",
        "ALTER TABLE notes ADD COLUMN multi_source INTEGER DEFAULT 0",
        "ALTER TABLE notes ADD COLUMN preprocessing TEXT DEFAULT 'raw'",
        "ALTER TABLE notes ADD COLUMN confirmations INTEGER DEFAULT 1",
    ]:
        try:
            _db_conn.execute(col_sql)
        except sqlite3.OperationalError:
            pass  # column already exists
    _db_conn.execute("CREATE INDEX IF NOT EXISTS idx_notes_source ON notes(source)")

    log.info("Database ready: %s", DB_PATH)


def insert_detection(det, clip_name, source="ground", preprocessing="raw", confirmations=1):
    """Insert a detection row into the database."""
    now = datetime.datetime.now()
    with _db_lock:
        _db_conn.execute(
            """
            INSERT INTO notes (source_node, date, time, common_name,
                               scientific_name, confidence, clip_name, input_file,
                               source, preprocessing, confirmations)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                source,
                now.strftime("%Y-%m-%d"),
                now.strftime("%H:%M:%S"),
                det["common_name"],
                det["scientific_name"],
                round(det["confidence"], 3),
                clip_name,
                "",
                source,
                preprocessing,
                confirmations,
            ),
        )
        _db_conn.commit()


# ── Audio Clip Saving ──────────────────────────────────────────────────────
def save_clip(raw_pcm, det):
    """Save a 3-second PCM chunk as a WAV file. Returns relative clip path."""
    now = datetime.datetime.utcnow()
    year_month = now.strftime("%Y/%m")
    clip_dir = CLIPS_DIR / year_month
    clip_dir.mkdir(parents=True, exist_ok=True)

    sci_name = det["scientific_name"].lower().replace(" ", "_")
    conf_pct = int(det["confidence"] * 100)
    ts = now.strftime("%Y%m%dT%H%M%SZ")
    filename = f"{sci_name}_{conf_pct}p_{ts}.wav"
    clip_path = clip_dir / filename

    with wave.open(str(clip_path), "wb") as wf:
        wf.setnchannels(CHANNELS)
        wf.setsampwidth(2)  # 16-bit
        wf.setframerate(SAMPLE_RATE)
        wf.writeframes(raw_pcm)

    return f"{year_month}/{filename}"


# ── Clip Cleanup ───────────────────────────────────────────────────────────
def cleanup_old_clips():
    """Delete clips older than CLIP_MAX_AGE_DAYS."""
    if not CLIPS_DIR.exists():
        return
    cutoff = time.time() - CLIP_MAX_AGE_DAYS * 86400
    removed = 0
    for wav_file in CLIPS_DIR.rglob("*.wav"):
        try:
            if wav_file.stat().st_mtime < cutoff:
                wav_file.unlink()
                removed += 1
        except OSError:
            pass
    if removed:
        log.info("Cleaned up %d old clips", removed)
    # Remove empty directories
    for dirpath in sorted(CLIPS_DIR.rglob("*"), reverse=True):
        if dirpath.is_dir():
            try:
                dirpath.rmdir()  # only works if empty
            except OSError:
                pass


# ── Main Analysis Loop ─────────────────────────────────────────────────────
def run(test_mode=False):
    """Main loop: pull audio, analyze, store results."""
    # Import here to keep startup fast for --help etc.
    from birdnetlib import Recording
    from birdnetlib.analyzer import Analyzer

    log.info("Loading BirdNET model...")
    t0 = time.time()
    analyzer = Analyzer()
    log.info("Model loaded in %.1fs", time.time() - t0)

    init_db()
    CLIPS_DIR.mkdir(parents=True, exist_ok=True)

    # Load range filter for geographic/habitat validation
    range_filter = None
    try:
        from range_filter import RangeFilter
        range_filter = RangeFilter()
        log.info("Range filter loaded (%d species)", len(range_filter.species_db))
    except Exception as e:
        log.warning("Range filter unavailable: %s", e)

    # JIT warmup: run one dummy inference to compile numba kernels
    log.info("Warming up inference engine...")
    dummy = np.zeros(SAMPLE_RATE * CHUNK_SECONDS, dtype=np.float32)
    try:
        from birdnetlib import RecordingBuffer
        with contextlib.redirect_stdout(io.StringIO()):
            warmup = RecordingBuffer(
                analyzer, dummy, SAMPLE_RATE,
                lat=LAT, lon=LON, min_conf=0.99,
            )
            warmup.analyze()
    except Exception:
        pass
    log.info("Warmup complete — ready for real-time analysis")

    # Schedule daily clip cleanup
    last_cleanup = 0

    total_detections = 0
    chunks_processed = 0
    dyn_thresh = DynamicThreshold() if DYNAMIC_THRESHOLD_ENABLED else None
    confirmer = OverlapConfirmation(
        flush_window=OVERLAP_FLUSH_WINDOW,
        min_confirmations=OVERLAP_MIN_CONFIRMATIONS,
    ) if OVERLAP_CONFIRMATION_ENABLED else None

    stream_mgr = RTSPStreamManager(
        service_name="analyzer",
        preferred_stream="ground",
        fallback_stream="birds",
    )

    while not _shutdown.is_set():
        # Sleep during nighttime — no birds calling, save CPU
        if is_nighttime():
            log.info("Nighttime — pausing analysis until sunrise")
            while is_nighttime() and not _shutdown.is_set():
                _shutdown.wait(60)  # check every minute
            if _shutdown.is_set():
                break
            log.info("Sunrise — resuming analysis")

        container = None
        try:
            container, audio_stream = stream_mgr.connect()
            stream_mgr.report_success()

            pcm_buf = bytearray()

            for frame in container.decode(audio_stream):
                if _shutdown.is_set():
                    break
                if is_nighttime():
                    break  # exit decode loop → outer loop will sleep

                # Decode to numpy, take channel 0 (stereo channels are identical),
                # convert to s16le bytes — no resampler needed, avoids frame-boundary artifacts
                arr = frame.to_ndarray()  # shape: (channels, samples), float planar
                mono = arr[0]  # take first channel
                pcm_samples = (mono * 32768.0).clip(-32768, 32767).astype(np.int16)
                pcm_buf.extend(pcm_samples.tobytes())

                # Process when we have a full analysis window
                while len(pcm_buf) >= ANALYSIS_BYTES:
                    raw = bytes(pcm_buf[:ANALYSIS_BYTES])
                    del pcm_buf[:ADVANCE_BYTES]
                    chunks_processed += 1
                    _metrics.counter("windows_processed").inc()
                    _metrics.gauge("pcm_buf_bytes").set(len(pcm_buf))

                    # Convert to float32 normalized [-1, 1]
                    audio_raw = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0

                    # Preprocess: bandpass filter + spectral noise reduction
                    t_pre = time.monotonic()
                    audio = preprocess_audio(audio_raw)
                    _metrics.histogram("preprocess_ms").record((time.monotonic() - t_pre) * 1000)

                    # SNR estimate: ratio of preprocessed RMS to raw RMS
                    raw_rms = float(np.sqrt(np.mean(audio_raw ** 2)))
                    clean_rms = float(np.sqrt(np.mean(audio ** 2)))
                    if raw_rms > 1e-10:
                        _metrics.gauge("snr_ratio").set(round(clean_rms / raw_rms, 3))

                    # Run BirdNET inference with overlapping 3s windows
                    # Timeout prevents pipeline stall if model hangs
                    t_inf_start = time.monotonic()
                    try:
                        recording = RecordingBuffer(
                            analyzer, audio, SAMPLE_RATE,
                            lat=LAT, lon=LON,
                            date=datetime.datetime.now(),
                            min_conf=0.25,  # pre-filter low to feed confirmer
                            overlap=0.0,  # no internal overlap — we handle it via sliding window
                        )
                        inference_result = [False]

                        def _run_inference():
                            with contextlib.redirect_stdout(io.StringIO()):
                                recording.analyze()
                            inference_result[0] = True

                        t_inf = threading.Thread(target=_run_inference, daemon=True)
                        t_inf.start()
                        t_inf.join(timeout=30)  # 30s max for inference
                        if not inference_result[0]:
                            log.error("Inference timed out (30s), skipping chunk")
                            _metrics.counter("inference_timeouts").inc()
                            continue
                    except Exception as e:
                        log.error("Inference error: %s", e)
                        _metrics.counter("inference_errors").inc()
                        continue

                    _metrics.histogram("inference_ms").record((time.monotonic() - t_inf_start) * 1000)

                    now_time = time.time()

                    # Per-slice dedup: keep only highest-confidence species per time slice
                    _metrics.counter("raw_detections").inc(len(recording.detections))
                    best_per_slice = {}
                    for det in recording.detections:
                        species = det["common_name"]
                        conf = det["confidence"]

                        # Apply dynamic threshold or base threshold
                        if dyn_thresh:
                            if not dyn_thresh.should_accept(species, conf):
                                _metrics.counter("rejected_threshold").inc()
                                continue
                        elif conf < MIN_CONFIDENCE:
                            _metrics.counter("rejected_threshold").inc()
                            continue

                        slice_key = det.get("start_time", 0)
                        existing = best_per_slice.get(slice_key)
                        if existing is None or conf > existing["confidence"]:
                            best_per_slice[slice_key] = det

                    for det in best_per_slice.values():
                        species = det["common_name"]
                        conf = det["confidence"]

                        # Overlap confirmation — add to pending, don't process directly
                        if confirmer:
                            confirmer.add(species, conf, det, now_time)
                            continue  # processing happens in flush below

                        # Range filter: reject impossible species for this location/habitat
                        if range_filter:
                            validation = range_filter.is_species_valid_at_location(
                                species, confidence=conf,
                                date=datetime.datetime.now()
                            )
                            if not validation["valid"]:
                                _metrics.counter("rejected_range").inc()
                                log.info(
                                    "Range filter rejected: %s (%.0f%%) — %s",
                                    species, conf * 100, validation["reason"],
                                )
                                continue

                        # Record for dynamic threshold learning
                        if dyn_thresh:
                            dyn_thresh.record_detection(species, conf)

                        # Extract the 3s clip from the analysis buffer
                        start_sec = det.get("start_time", 0)
                        clip_start = int(start_sec * SAMPLE_RATE * 2)
                        clip_end = clip_start + CHUNK_BYTES
                        if clip_end > len(raw):
                            clip_start = max(0, len(raw) - CHUNK_BYTES)
                            clip_end = len(raw)
                        clip_raw = raw[clip_start:clip_end]

                        try:
                            clip_name = save_clip(clip_raw, det)
                        except Exception as e:
                            log.warning("Failed to save clip: %s", e)
                            clip_name = ""
                        t_db = time.monotonic()
                        insert_detection(det, clip_name)
                        _metrics.histogram("db_write_ms").record((time.monotonic() - t_db) * 1000)
                        _metrics.counter("accepted").inc()
                        _metrics.histogram("accepted_confidence").record(conf)
                        total_detections += 1
                        log.info(
                            "Detection #%d: %s (%.0f%%) — %s",
                            total_detections,
                            species,
                            conf * 100,
                            clip_name,
                        )

                    # Flush confirmed detections from overlap confirmation
                    if confirmer:
                        for confirmed_det in confirmer.flush(now_time):
                            species = confirmed_det["common_name"]
                            conf = confirmed_det["confidence"]
                            confirmations = confirmed_det.get("confirmations", 1)

                            # Range filter
                            if range_filter:
                                validation = range_filter.is_species_valid_at_location(
                                    species, confidence=conf,
                                    date=datetime.datetime.now()
                                )
                                if not validation["valid"]:
                                    _metrics.counter("rejected_range").inc()
                                    log.info("Range filter rejected: %s (%.0f%%) — %s",
                                             species, conf * 100, validation["reason"])
                                    continue

                            if dyn_thresh:
                                dyn_thresh.record_detection(species, conf)

                            start_sec = confirmed_det.get("start_time", 0)
                            clip_start = int(start_sec * SAMPLE_RATE * 2)
                            clip_end = clip_start + CHUNK_BYTES
                            if clip_end > len(raw):
                                clip_start = max(0, len(raw) - CHUNK_BYTES)
                                clip_end = len(raw)
                            clip_raw = raw[clip_start:clip_end]

                            try:
                                clip_name = save_clip(clip_raw, confirmed_det)
                            except Exception as e:
                                log.warning("Failed to save clip: %s", e)
                                clip_name = ""
                            insert_detection(confirmed_det, clip_name,
                                             source="ground", confirmations=confirmations)
                            _metrics.counter("accepted").inc()
                            total_detections += 1
                            log.info("Detection #%d: %s (%.0f%%, %d confirms) — %s",
                                     total_detections, species, conf * 100,
                                     confirmations, clip_name)

                    if test_mode:
                        log.info("Test mode: processed 1 analysis window, exiting")
                        container.close()
                        return

                    # Periodic cleanup (once per day)
                    now_ts = time.time()
                    if now_ts - last_cleanup > 86400:
                        last_cleanup = now_ts
                        threading.Thread(target=cleanup_old_clips, daemon=True).start()

                    # Log progress periodically
                    if chunks_processed % 100 == 0:
                        log.info(
                            "Processed %d windows, %d total detections",
                            chunks_processed, total_detections,
                        )

                # Recovery probes while on fallback
                if stream_mgr.should_probe():
                    if stream_mgr.probe_primary():
                        if stream_mgr.should_switch_to_primary():
                            log.info("Primary stream recovered, switching back")
                            stream_mgr.switch_to_primary()
                            break  # exit decode loop to reconnect on primary

            log.warning("RTSP stream ended")

        except Exception as e:
            log.error("Stream error: %s", e)
            stream_mgr.report_failure(e)
        finally:
            if container:
                try:
                    container.close()
                except Exception:
                    pass

        if not _shutdown.is_set():
            stream_mgr.wait_backoff(_shutdown)


# ── Metrics HTTP Server ───────────────────────────────────────────────────
from http.server import HTTPServer, BaseHTTPRequestHandler

METRICS_PORT = int(os.environ.get("BIRDNET_METRICS_PORT", "8098"))


class _MetricsHandler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def do_GET(self):
        if self.path == '/metrics':
            data = json.dumps(_metrics.snapshot()).encode()
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        elif self.path == '/health':
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(b'{"status":"ok"}')
        else:
            self.send_error(404)


def _start_metrics_server():
    """Start a background HTTP server for metrics on port 8098."""
    try:
        srv = HTTPServer(("0.0.0.0", METRICS_PORT), _MetricsHandler)
        t = threading.Thread(target=srv.serve_forever, daemon=True)
        t.start()
        log.info("Metrics server on port %d", METRICS_PORT)
    except Exception as e:
        log.warning("Could not start metrics server: %s", e)


# ── Signal Handling ────────────────────────────────────────────────────────
def _handle_signal(signum, frame):
    log.info("Received signal %d, shutting down...", signum)
    _shutdown.set()


def main():
    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)
    _start_metrics_server()

    test_mode = "--test" in sys.argv

    log.info("Bird Audio Analyzer starting")
    log.info("  RTSP: managed (preferred=ground, fallback=birds)")
    log.info("  Location: %.2f, %.2f", LAT, LON)
    log.info("  Min confidence: %.0f%%", MIN_CONFIDENCE * 100)
    log.info("  Dynamic threshold floor: %.0f%%", DYNAMIC_THRESHOLD_MIN * 100)
    if OVERLAP_CONFIRMATION_ENABLED:
        log.info("  Overlap confirmation: %ds window, %d min confirmations",
                 OVERLAP_FLUSH_WINDOW, OVERLAP_MIN_CONFIRMATIONS)
    log.info("  Noise reduction: %s", "ON (bandpass + noisereduce + RMS match)" if NOISE_REDUCE_ENABLED else "bandpass only")
    log.info("  DB: %s", DB_PATH)
    log.info("  Clips: %s", CLIPS_DIR)

    try:
        run(test_mode=test_mode)
    except Exception as e:
        log.error("Fatal error: %s", e, exc_info=True)
        sys.exit(1)

    log.info("Shutdown complete")


if __name__ == "__main__":
    main()
