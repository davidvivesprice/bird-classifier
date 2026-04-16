"""HlsRecorder — dedicated ffmpeg subprocess for HLS chunk recording."""
from __future__ import annotations
import logging
import subprocess
import threading
import time
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

FFMPEG = "/usr/local/bin/ffmpeg"


class HlsRecorder:
    def __init__(self, camera_name: str, rtsp_url: str, output_dir: str):
        self.camera_name = camera_name
        self.rtsp_url = rtsp_url
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.proc: Optional[subprocess.Popen] = None
        self._watchdog: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self.stats = {"chunks_written": 0, "restarts": 0, "last_chunk_ms": None}

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

    def stop(self):
        self._stop.set()
        if self._watchdog is not None:
            self._watchdog.join(timeout=2)
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
