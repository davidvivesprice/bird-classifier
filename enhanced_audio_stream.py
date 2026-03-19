#!/usr/bin/env python3
"""Enhanced audio stream server.

Captures RTSP audio via python-av, applies a bandpass filter (300Hz-15kHz)
to isolate bird call frequencies, encodes to MP3 via ffmpeg, and serves
as a streaming HTTP endpoint. The dashboard toggles between this and the
raw camera feed.

Architecture:
  RTSP → python-av (decode) → bandpass filter → ring buffer → ffmpeg (encode) → HTTP
"""

import collections
import logging
import os
import select
import signal
import subprocess
import threading
import time
from http.server import HTTPServer, BaseHTTPRequestHandler

import av
import numpy as np
from scipy.signal import butter, sosfilt, sosfilt_zi

from metrics import MetricsRegistry
_metrics = MetricsRegistry()

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

# Reconnection backoff
RECONNECT_BASE = 3
RECONNECT_MAX = 30

# ── Audio preprocessing ───────────────────────────────────────────────────
# Simple bandpass filter — removes wind/traffic rumble (<300Hz) and
# ultrasonic noise (>15kHz), leaving the bird call frequency range clean.
BANDPASS_LOW = 300
BANDPASS_HIGH = 15000
BANDPASS_ORDER = 4
_bandpass_sos = butter(BANDPASS_ORDER, [BANDPASS_LOW, BANDPASS_HIGH],
                       btype='band', fs=SAMPLE_RATE, output='sos')

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


def _rtsp_reader():
    """Background thread: decode RTSP audio, apply bandpass, push to ring buffer."""
    global _ring_seq

    reconnect_delay = RECONNECT_BASE

    while not _shutdown.is_set():
        container = None
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
                continue

            log.info("Audio stream: %s %dHz %dch",
                     audio_stream.codec_context.name,
                     audio_stream.codec_context.sample_rate,
                     audio_stream.codec_context.channels)

            resampler = av.AudioResampler(
                format="s16", layout="mono", rate=SAMPLE_RATE
            )

            # Accumulate audio frames in a list (avoid O(n²) np.concatenate)
            pcm_parts = []
            pcm_parts_samples = 0
            chunks_produced = 0

            # Initialize bandpass filter state for seamless chunk boundaries
            zi = sosfilt_zi(_bandpass_sos)

            # Reset backoff on successful connection
            reconnect_delay = RECONNECT_BASE

            for packet in container.demux(audio_stream):
                if _shutdown.is_set():
                    break
                for frame in packet.decode():
                    resampled = resampler.resample(frame)
                    for rf in resampled:
                        raw = rf.to_ndarray().flatten()
                        audio = raw.astype(np.float32) / 32768.0
                        pcm_parts.append(audio)
                        pcm_parts_samples += len(audio)

                        while pcm_parts_samples >= CHUNK_SAMPLES:
                            # Concatenate once to extract chunk
                            pcm_buf = np.concatenate(pcm_parts)
                            chunk = pcm_buf[:CHUNK_SAMPLES]
                            remainder = pcm_buf[CHUNK_SAMPLES:]

                            # Rebuild parts list from remainder
                            if len(remainder) > 0:
                                pcm_parts = [remainder]
                                pcm_parts_samples = len(remainder)
                            else:
                                pcm_parts = []
                                pcm_parts_samples = 0

                            # Apply bandpass filter with state persistence
                            filtered, zi = sosfilt(_bandpass_sos, chunk, zi=zi)
                            pcm_bytes = (filtered * 32768).astype(np.int16).tobytes()

                            # Push to ring buffer
                            with _ring_cond:
                                _ring_buf.append((_ring_seq, pcm_bytes))
                                _ring_seq += 1
                                _ring_cond.notify_all()
                            _metrics.counter("chunks_produced").inc()
                            _metrics.gauge("ring_buffer_items").set(len(_ring_buf))

                            if not _stream_ready.is_set():
                                _stream_ready.set()
                                log.info("Stream ready, first chunk processed")

                            chunks_produced += 1
                            if chunks_produced % 60 == 0:
                                log.info("Processed %d chunks (%.0fs)", chunks_produced, chunks_produced * CHUNK_SECONDS)

        except av.error.ExitError:
            log.warning("RTSP stream ended")
        except Exception as e:
            log.error("RTSP error: %s", e)
        finally:
            if container:
                try:
                    container.close()
                except Exception:
                    pass

        if not _shutdown.is_set():
            _metrics.counter("reconnects").inc()
            log.info("Reconnecting in %ds...", reconnect_delay)
            _shutdown.wait(reconnect_delay)
            reconnect_delay = min(reconnect_delay * 2, RECONNECT_MAX)


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

        if self.path == "/metrics":
            import json
            data = json.dumps(_metrics.snapshot()).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return

        if self.path != "/stream.mp3":
            self.send_response(404)
            self.end_headers()
            return

        _metrics.counter("client_connects").inc()
        log.info("Client connected: %s", self.client_address[0])

        # Wait for stream to be ready (up to 10s)
        if not _stream_ready.wait(timeout=10):
            log.warning("Stream not ready, rejecting client")
            self.send_response(503)
            self.end_headers()
            return

        # Start ffmpeg encoder: raw PCM → MP3 stream
        # stderr=DEVNULL prevents pipe buffer deadlock when ffmpeg emits warnings
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
        ], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)

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
        bytes_sent = 0
        try:
            while not _shutdown.is_set():
                # Use select to avoid blocking forever if ffmpeg stalls
                ready, _, _ = select.select([encoder.stdout], [], [], 5.0)
                if not ready:
                    if encoder.poll() is not None:
                        break
                    continue
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
            # Graceful encoder cleanup: terminate, then kill if stuck
            encoder.terminate()
            try:
                encoder.wait(timeout=2)
            except subprocess.TimeoutExpired:
                encoder.kill()
                encoder.wait()
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
    log.info("  Filter: bandpass %d-%d Hz", BANDPASS_LOW, BANDPASS_HIGH)
    log.info("  Chunk: %.1fs, Ring: %d chunks", CHUNK_SECONDS, RING_SIZE)

    # Start RTSP reader thread (non-daemon so we can join on shutdown)
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
        _shutdown.set()
        server.server_close()
        reader.join(timeout=5)
        log.info("Server stopped")


if __name__ == "__main__":
    main()
