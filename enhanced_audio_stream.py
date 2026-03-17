#!/usr/bin/env python3
"""Enhanced audio stream server.

Captures RTSP audio via python-av, applies a wind-resilient noise reduction
pipeline, encodes to MP3 via ffmpeg, and serves as a streaming HTTP endpoint.
The dashboard toggles between this and the raw camera feed.

Architecture:
  RTSP → python-av (decode) → preprocess → crossfade → ring buffer → ffmpeg (encode) → HTTP

Audio preprocessing pipeline (optimized for coastal wind noise):
  1. Bandpass filter (300Hz–15kHz) — removes sub-bass rumble and ultrasonic noise
  2. Wind detection — if low-freq energy (<200Hz) dominates, skip noisereduce entirely
  3. noisereduce (non-stationary mode) — adaptive spectral gating with gentle parameters
     tuned to avoid gate-switching artifacts during wind gusts
  4. RMS normalization — restores original signal level, gain capped at 3× (9.5 dB)
  5. Chunk crossfade — Hann window overlap at chunk boundaries prevents clicks

This pipeline differs from audio_analyzer.py which uses aggressive settings
(prop_decrease=0.85, stationary mode) because BirdNET detection needs maximum
noise suppression and doesn't care about audio quality. This stream is for
human ears — artifacts must never be audible.
"""

import collections
import io
import logging
import os
import signal
import socket
import struct
import subprocess
import sys
import threading
import time
from http.server import HTTPServer, BaseHTTPRequestHandler

import av
import numpy as np
from scipy.signal import butter, sosfilt

# ── Configuration ──────────────────────────────────────────────────────────
RTSP_URL = os.environ.get(
    "RTSP_URL",
    "rtsp://192.168.4.9:7447/VaeaRCXUbGgJsYSA",
)
SAMPLE_RATE = 48000
CHANNELS = 1
CHUNK_SECONDS = 1.0
CHUNK_SAMPLES = int(SAMPLE_RATE * CHUNK_SECONDS)

HTTP_PORT = int(os.environ.get("ENHANCED_AUDIO_PORT", "8096"))
MP3_BITRATE = "192k"

# Ring buffer: holds processed PCM chunks for clients to read
RING_SIZE = 30  # ~30 seconds of audio

# ── Audio preprocessing (tuned for listener comfort, NOT detection) ────────
# Unlike audio_analyzer.py (aggressive for BirdNET), this prioritizes
# pleasant audio: no clicking, no distortion, even in extreme wind.

BANDPASS_LOW = 300
BANDPASS_HIGH = 15000
BANDPASS_ORDER = 4
_bandpass_sos = butter(BANDPASS_ORDER, [BANDPASS_LOW, BANDPASS_HIGH],
                       btype='band', fs=SAMPLE_RATE, output='sos')

# Wind detection: 200Hz lowpass to measure sub-bass energy from wind gusts
WIND_DETECT_CUTOFF = 200
WIND_RATIO_THRESHOLD = 3.0  # skip noisereduce when wind energy > 3× passband
_wind_detect_sos = butter(2, WIND_DETECT_CUTOFF, btype='low',
                          fs=SAMPLE_RATE, output='sos')

# RMS normalization gain cap (prevents amplifying residual artifacts)
MAX_RMS_GAIN = 3.0  # 9.5 dB max boost

# Chunk crossfade to eliminate boundary clicks
FADE_SAMPLES = 2048  # ~42ms at 48kHz — imperceptible transition
_prev_tail = None

NOISE_REDUCE_ENABLED = True
try:
    import noisereduce as nr
except ImportError:
    nr = None
    NOISE_REDUCE_ENABLED = False

# ── Logging ────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("enhanced_audio")

# ── Globals ────────────────────────────────────────────────────────────────
_shutdown = threading.Event()

# Shared ring buffer of processed PCM chunks (each is bytes of int16 samples)
_ring_lock = threading.Lock()
_ring_buf = collections.deque(maxlen=RING_SIZE)
_ring_seq = 0       # monotonic sequence counter
_ring_cond = threading.Condition(_ring_lock)  # notify clients of new data
_stream_ready = threading.Event()  # set once first chunk is in the buffer


def preprocess_chunk(audio_float32):
    """Apply wind-resilient noise reduction for human listening.

    Pipeline:
      1. Bandpass (300Hz–15kHz) — removes rumble and ultrasonic noise
      2. Wind check — if sub-bass energy dominates, skip noisereduce
      3. noisereduce (non-stationary, gentle) — adaptive spectral gating
      4. RMS normalization — restore volume, gain capped at 3×
    """
    original_rms = np.sqrt(np.mean(audio_float32 ** 2))
    if original_rms < 1e-10:
        return audio_float32

    # Stage 1: Bandpass filter
    cleaned = sosfilt(_bandpass_sos, audio_float32).astype(np.float32)

    # Stage 2: Wind detection — measure sub-bass energy from raw audio
    # Wind gusts produce massive energy below 200Hz; when this dominates,
    # noisereduce creates the worst artifacts, so we skip it entirely.
    skip_nr = False
    if NOISE_REDUCE_ENABLED and nr is not None:
        wind_energy = np.sqrt(np.mean(sosfilt(_wind_detect_sos, audio_float32) ** 2))
        band_energy = np.sqrt(np.mean(cleaned ** 2))
        wind_ratio = wind_energy / (band_energy + 1e-10)
        if wind_ratio > WIND_RATIO_THRESHOLD:
            skip_nr = True

    # Stage 3: Noise reduction (non-stationary mode, gentle parameters)
    if NOISE_REDUCE_ENABLED and nr is not None and not skip_nr:
        cleaned = nr.reduce_noise(
            y=cleaned, sr=SAMPLE_RATE,
            stationary=False,
            n_fft=1024,
            prop_decrease=0.40,
            time_constant_s=3.0,
            freq_mask_smooth_hz=1000,
            time_mask_smooth_ms=120,
            thresh_n_mult_nonstationary=3.5,
            sigmoid_slope_nonstationary=6,
        ).astype(np.float32)

    # Stage 4: RMS normalization with gain cap
    cleaned_rms = np.sqrt(np.mean(cleaned ** 2))
    if cleaned_rms > 1e-10:
        gain = min(original_rms / cleaned_rms, MAX_RMS_GAIN)
        cleaned = cleaned * gain
        np.clip(cleaned, -1.0, 1.0, out=cleaned)

    return cleaned


def _crossfade_chunk(processed):
    """Apply Hann window crossfade at chunk boundaries to eliminate clicks.

    Each chunk's first FADE_SAMPLES are blended with the previous chunk's
    last FADE_SAMPLES using a Hann window taper.
    """
    global _prev_tail

    if _prev_tail is not None and len(processed) >= FADE_SAMPLES:
        fade_in = np.linspace(0, 1, FADE_SAMPLES, dtype=np.float32)
        fade_out = 1.0 - fade_in
        processed[:FADE_SAMPLES] = (
            _prev_tail * fade_out + processed[:FADE_SAMPLES] * fade_in
        )

    if len(processed) >= FADE_SAMPLES:
        _prev_tail = processed[-FADE_SAMPLES:].copy()
    else:
        _prev_tail = None

    return processed


def _rtsp_reader():
    """Background thread: decode RTSP audio, preprocess, push to ring buffer."""
    global _ring_seq

    while not _shutdown.is_set():
        try:
            log.info("Opening RTSP stream: %s", RTSP_URL)
            container = av.open(RTSP_URL, options={
                "rtsp_transport": "tcp",
                "stimeout": "5000000",
            })
            audio_stream = None
            for s in container.streams:
                if s.type == "audio":
                    audio_stream = s
                    break

            if not audio_stream:
                log.error("No audio stream found in RTSP")
                time.sleep(5)
                continue

            log.info("Audio stream: %s %dHz %dch",
                     audio_stream.codec_context.name,
                     audio_stream.codec_context.sample_rate,
                     audio_stream.codec_context.channels)

            resampler = av.AudioResampler(
                format="s16", layout="mono", rate=SAMPLE_RATE
            )

            pcm_buf = np.array([], dtype=np.float32)
            chunks_produced = 0

            for packet in container.demux(audio_stream):
                if _shutdown.is_set():
                    break
                for frame in packet.decode():
                    resampled = resampler.resample(frame)
                    for rf in resampled:
                        raw = rf.to_ndarray().flatten()
                        audio = raw.astype(np.float32) / 32768.0
                        pcm_buf = np.concatenate([pcm_buf, audio])

                        while len(pcm_buf) >= CHUNK_SAMPLES:
                            chunk = pcm_buf[:CHUNK_SAMPLES]
                            pcm_buf = pcm_buf[CHUNK_SAMPLES:]

                            # Preprocess the chunk + crossfade boundaries
                            processed = preprocess_chunk(chunk)
                            processed = _crossfade_chunk(processed)
                            pcm_bytes = (processed * 32768).astype(np.int16).tobytes()

                            # Push to ring buffer
                            with _ring_cond:
                                _ring_buf.append((_ring_seq, pcm_bytes))
                                _ring_seq += 1
                                _ring_cond.notify_all()

                            if not _stream_ready.is_set():
                                _stream_ready.set()
                                log.info("Stream ready, first chunk processed")

                            chunks_produced += 1
                            if chunks_produced % 60 == 0:
                                log.info("Processed %d chunks (%.0fs)", chunks_produced, chunks_produced * CHUNK_SECONDS)

            container.close()

        except av.error.ExitError:
            log.warning("RTSP stream ended")
        except Exception as e:
            log.error("RTSP error: %s", e)

        if not _shutdown.is_set():
            log.info("Reconnecting in 3s...")
            time.sleep(3)


class StreamHandler(BaseHTTPRequestHandler):
    """HTTP handler that serves the enhanced audio stream."""

    def do_GET(self):
        if self.path == "/health":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            ready = "true" if _stream_ready.is_set() else "false"
            self.wfile.write(f'{{"status":"ok","stream_ready":{ready}}}'.encode())
            return

        if self.path != "/stream.mp3":
            self.send_response(404)
            self.end_headers()
            return

        log.info("Client connected: %s", self.client_address[0])

        # Wait for stream to be ready (up to 10s)
        if not _stream_ready.wait(timeout=10):
            log.warning("Stream not ready, rejecting client")
            self.send_response(503)
            self.end_headers()
            return

        # Start ffmpeg encoder: raw PCM → MP3 stream
        encoder = subprocess.Popen([
            "/usr/local/bin/ffmpeg", "-hide_banner", "-loglevel", "warning",
            "-f", "s16le",
            "-ar", str(SAMPLE_RATE),
            "-ac", str(CHANNELS),
            "-i", "pipe:0",
            "-codec:a", "libmp3lame",
            "-b:a", MP3_BITRATE,
            "-f", "mp3",
            "pipe:1",
        ], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

        # Send HTTP headers
        self.send_response(200)
        self.send_header("Content-Type", "audio/mpeg")
        self.send_header("Cache-Control", "no-cache, no-store")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()

        def feed_encoder():
            """Feed processed PCM from ring buffer into ffmpeg encoder."""
            try:
                # Start from the latest chunk in the ring buffer
                with _ring_lock:
                    last_seq = _ring_seq - 1 if _ring_seq > 0 else -1

                while not _shutdown.is_set():
                    with _ring_cond:
                        # Wait for new data
                        while not _shutdown.is_set():
                            # Find chunks newer than last_seq
                            new_chunks = [(s, d) for s, d in _ring_buf if s > last_seq]
                            if new_chunks:
                                break
                            _ring_cond.wait(timeout=1.0)

                        if _shutdown.is_set():
                            break

                    # Write new chunks to encoder
                    for seq, pcm_data in new_chunks:
                        try:
                            encoder.stdin.write(pcm_data)
                            encoder.stdin.flush()
                        except BrokenPipeError:
                            return
                        last_seq = seq

            except Exception as e:
                log.warning("Feed thread error: %s", e)
            finally:
                try:
                    encoder.stdin.close()
                except Exception:
                    pass

        feed_thread = threading.Thread(target=feed_encoder, daemon=True)
        feed_thread.start()

        # Stream MP3 from encoder to client
        try:
            bytes_sent = 0
            while not _shutdown.is_set():
                data = encoder.stdout.read(4096)
                if not data:
                    if encoder.poll() is not None:
                        break
                    continue
                self.wfile.write(data)
                self.wfile.flush()
                bytes_sent += len(data)
        except (BrokenPipeError, ConnectionResetError):
            log.info("Client disconnected: %s (sent %dKB)", self.client_address[0], bytes_sent // 1024)
        except Exception as e:
            log.warning("Stream error for %s: %s", self.client_address[0], e)
        finally:
            encoder.terminate()
            feed_thread.join(timeout=2)
            log.info("Stream ended for %s (sent %dKB)", self.client_address[0], bytes_sent // 1024)

    def log_message(self, format, *args):
        pass


class ThreadedHTTPServer(HTTPServer):
    """Handle each request in a new thread."""
    allow_reuse_address = True

    def process_request(self, request, client_address):
        t = threading.Thread(target=self._handle, args=(request, client_address), daemon=True)
        t.start()

    def _handle(self, request, client_address):
        try:
            self.finish_request(request, client_address)
        except Exception:
            self.handle_error(request, client_address)
        finally:
            self.shutdown_request(request)


def main():
    log.info("Enhanced audio stream server starting on port %d", HTTP_PORT)
    log.info("  RTSP: %s", RTSP_URL)
    log.info("  MP3 bitrate: %s", MP3_BITRATE)
    log.info("  Noise reduction: %s", "ON (non-stationary, prop=0.40, wind bypass)" if NOISE_REDUCE_ENABLED else "OFF")
    log.info("  Wind bypass threshold: %.1f", WIND_RATIO_THRESHOLD)
    log.info("  RMS gain cap: %.1f×", MAX_RMS_GAIN)
    log.info("  Crossfade: %d samples (%.1fms)", FADE_SAMPLES, FADE_SAMPLES / SAMPLE_RATE * 1000)
    log.info("  Chunk: %.1fs, Ring: %d chunks", CHUNK_SECONDS, RING_SIZE)

    # Start RTSP reader thread
    reader = threading.Thread(target=_rtsp_reader, daemon=True)
    reader.start()

    server = ThreadedHTTPServer(("0.0.0.0", HTTP_PORT), StreamHandler)

    def shutdown_handler(signum, frame):
        log.info("Shutting down...")
        _shutdown.set()
        with _ring_cond:
            _ring_cond.notify_all()
        server.shutdown()

    signal.signal(signal.SIGTERM, shutdown_handler)
    signal.signal(signal.SIGINT, shutdown_handler)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        log.info("Server stopped")


if __name__ == "__main__":
    main()
