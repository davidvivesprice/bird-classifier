"""HlsRecorder — dedicated ffmpeg subprocess for HLS chunk recording.

Also maintains `segments.json` sidecar with iMac wall-clock stamps per
segment, used by the browser overlay for drift-proof sync (bypasses
ffmpeg's PDT which is anchored to ffmpeg-start, not to frame arrival).
"""
from __future__ import annotations
import json
import logging
import subprocess
import threading
import time
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

import shutil as _shutil
FFMPEG = _shutil.which("ffmpeg") or "/usr/local/bin/ffmpeg"

# How often the sidecar-manifest thread polls the HLS directory for
# segment changes. Small enough that segment completions are recorded
# within a fraction of a second; large enough that it's negligible CPU.
MANIFEST_POLL_INTERVAL_S = 0.25

# How long after the last write we consider a segment "complete."
# ffmpeg writes a .ts then moves on; mtime stops changing. We wait this
# long before recording the completion time to be sure no more writes.
MANIFEST_SETTLE_S = 0.5


class HlsRecorder:
    def __init__(self, camera_name: str, rtsp_url: str, output_dir: str):
        self.camera_name = camera_name
        self.rtsp_url = rtsp_url
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.proc: Optional[subprocess.Popen] = None
        self._watchdog: Optional[threading.Thread] = None
        self._manifest_thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self.stats = {
            "chunks_written": 0,
            "restarts": 0,
            "last_chunk_ms": None,
            "manifest_updates": 0,
        }

    def _build_cmd(self) -> list:
        playlist = self.output_dir / "live.m3u8"
        segment = self.output_dir / "seg_%Y%m%d-%H%M%S.ts"
        return [
            FFMPEG,
            "-loglevel", "warning",
            "-rtsp_transport", "tcp",
            "-i", self.rtsp_url,
            "-c", "copy",
            "-f", "hls",
            "-hls_time", "2",
            "-hls_list_size", "15",
            "-hls_flags", "delete_segments+program_date_time",
            "-strftime", "1",
            "-hls_segment_filename", str(segment),
            str(playlist),
        ]

    def start(self):
        self._stop.clear()
        self._spawn()
        self._watchdog = threading.Thread(
            target=self._watchdog_loop, name=f"hls-wd-{self.camera_name}", daemon=True
        )
        self._watchdog.start()
        self._manifest_thread = threading.Thread(
            target=self._manifest_loop, name=f"hls-mf-{self.camera_name}", daemon=True
        )
        self._manifest_thread.start()

    def stop(self):
        self._stop.set()
        if self._watchdog is not None:
            self._watchdog.join(timeout=2)
        if self._manifest_thread is not None:
            self._manifest_thread.join(timeout=2)
        proc = self.proc
        if proc is not None:
            try:
                proc.kill()
                proc.wait(timeout=5)
            except Exception:
                pass

    def _spawn(self):
        cmd = self._build_cmd()
        log.info("[%s] HLS recorder: %s", self.camera_name, " ".join(cmd))
        try:
            self.proc = subprocess.Popen(
                cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
            )
        except Exception as e:
            log.error("[%s] HLS recorder spawn failed: %s", self.camera_name, e)
            self.proc = None

    def _watchdog_loop(self):
        while not self._stop.is_set():
            try:
                time.sleep(5)
                if self._stop.is_set():
                    break
                proc = self.proc
                if proc is None or proc.poll() is not None:
                    log.warning("[%s] HLS recorder dead, respawning",
                                self.camera_name)
                    self.stats["restarts"] += 1
                    time.sleep(2)
                    self._spawn()
            except Exception:
                log.exception("[%s] HLS watchdog error", self.camera_name)
                time.sleep(1)

    def _manifest_loop(self):
        """Maintain `segments.json` sidecar with iMac wall-clock stamps.

        For each .ts segment in the output directory, record:
          - file: filename (not path)
          - completed_ms: iMac wall-clock (ms since epoch) when the segment
                          finished being written. Stamped the first poll
                          after the file has been stable (no size change)
                          for MANIFEST_SETTLE_S.

        The browser uses this instead of ffmpeg's PDT for overlay sync.
        PDT is unreliable because it's anchored to ffmpeg start time, not
        to frame arrival — see spec:
        docs/superpowers/specs/2026-04-16-overlay-sync-ground-truth-verification.md
        """
        # Per-file state: {filename: {"size": int, "size_ts": float, "completed_ms": int|None}}
        tracked: dict = {}
        while not self._stop.is_set():
            try:
                now = time.time()
                current_files = sorted(self.output_dir.glob("seg_*.ts"))
                current_names = {p.name for p in current_files}

                # Update tracked state
                for p in current_files:
                    try:
                        st = p.stat()
                    except FileNotFoundError:
                        continue
                    size = st.st_size
                    name = p.name
                    if name not in tracked:
                        tracked[name] = {
                            "size": size,
                            "size_ts": now,
                            "completed_ms": None,
                        }
                    else:
                        entry = tracked[name]
                        if entry["size"] != size:
                            # Still growing — bump size, reset settle timer
                            entry["size"] = size
                            entry["size_ts"] = now
                        elif (
                            entry["completed_ms"] is None
                            and (now - entry["size_ts"]) >= MANIFEST_SETTLE_S
                        ):
                            # Settled: record completion time as the mtime
                            # (more accurate than "now", since polling can lag).
                            entry["completed_ms"] = int(st.st_mtime * 1000)
                            self.stats["chunks_written"] += 1
                            self.stats["last_chunk_ms"] = entry["completed_ms"]

                # Prune deleted segments
                for name in list(tracked.keys()):
                    if name not in current_names:
                        del tracked[name]

                # Write manifest (only include files whose completion is known)
                segments = [
                    {"file": name, "completed_ms": tracked[name]["completed_ms"]}
                    for name in sorted(tracked.keys())
                    if tracked[name]["completed_ms"] is not None
                ]
                manifest = {
                    "camera": self.camera_name,
                    "updated_ms": int(now * 1000),
                    "segments": segments,
                }
                manifest_path = self.output_dir / "segments.json"
                tmp_path = manifest_path.with_suffix(".json.tmp")
                with open(tmp_path, "w") as f:
                    json.dump(manifest, f, separators=(",", ":"))
                tmp_path.replace(manifest_path)
                self.stats["manifest_updates"] += 1
            except Exception:
                log.exception("[%s] manifest loop error", self.camera_name)
            # Sleep with stop check granularity
            for _ in range(int(MANIFEST_POLL_INTERVAL_S * 10)):
                if self._stop.is_set():
                    return
                time.sleep(0.1)

    @staticmethod
    def cleanup_old_chunks(hls_root, retention_days: int = 7):
        """Delete HLS segments older than retention_days."""
        hls_root = Path(hls_root)
        if not hls_root.exists():
            return
        cutoff = time.time() - retention_days * 86400
        for camera_dir in hls_root.iterdir():
            if not camera_dir.is_dir():
                continue
            for seg in camera_dir.glob("*.ts"):
                try:
                    if seg.stat().st_mtime < cutoff:
                        seg.unlink()
                except Exception:
                    pass
