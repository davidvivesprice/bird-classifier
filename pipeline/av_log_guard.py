"""PyAV av_log() SEGV guard — import this module BEFORE any av.open().

ROOT CAUSE (confirmed across ~70 core dumps, identical backtrace every time):
    av_vlog  <-  av_log  <-  av_read_frame       (on the decode thread)
libav's logging path is not thread-safe. When the decode thread logs (any
decode warning/error — a flaky RTSP stream, a looped file at EOF) concurrently
with the process's other threads, it segfaults *inside av_vlog*, upstream of any
level filter — so PyAV's default level=None ("ignore all") does NOT protect
against it. Every crash restarts the pipeline and wipes all tracks: a real cause
of the live view "losing the bird" (observed every ~24s under load pre-fix).

!!! PROVEN INEFFECTIVE (2026-07-01) — DO NOT TRUST THIS AS "THE FIX" !!!
Empirically tested FOUR log-callback states under the 1080p-replay reproducer
(which crashes ~2×/140s): PyAV's no-op `nolog_callback` (the default),
`log_callback` (Python), libav's C `av_log_default_callback`, and NULL (below).
ALL FOUR still SEGV at the same rate. The crash is INSIDE av_vlog *before* the
callback is invoked (the `avcl` dereference / libav's own log path), so no
callback swap can prevent it. This is a native libav bug in the decode/read
path, NOT a logging-config issue. The real fix is decode-process isolation (run
FrameCapture in a child process so a decode SEGV can't wipe the tracker/lock
state). See docs/working/progress/cross-claude-comms.md.

This module is retained only to (a) silence libav's log spam and (b) document
the dead end. It does NOT stop the crash. Historical attempt below:

THE ATTEMPT: set libav's C log callback to NULL, so av_vlog() has nothing to
invoke — no Python callback from a C thread, no default-callback static state.
Done via ctypes against the exact libavutil PyAV loaded. Tradeoff: libav log
lines are silenced. (Did not help — the crash is upstream of the callback.)

Lives in its own tiny module so EVERY process that decodes video — the pipeline
entry point, FrameCapture, and any offline/replay tool — installs the guard
before the first av.open(), regardless of import order.
"""
import av.logging as _L

# Belt: disable the racy repeated-message suppressor.
try:
    _L.set_skip_repeated(False)
except Exception:
    pass


def _install_null_av_log_callback():
    """Set libav's log callback to NULL via ctypes — the definitive fix."""
    import ctypes
    import glob
    import os
    import av
    d = os.path.dirname(av.__file__)
    cands = (glob.glob(os.path.join(d, "..", "av.libs", "libavutil*")) +
             glob.glob(os.path.join(d, ".dylibs", "libavutil*")))
    lib = ctypes.CDLL(cands[0]) if cands else ctypes.CDLL(None)
    lib.av_log_set_callback.argtypes = [ctypes.c_void_p]
    lib.av_log_set_callback.restype = None
    lib.av_log_set_callback(None)


try:
    _install_null_av_log_callback()
except Exception:
    # Fallback: at least hand logging to libav's plain C callback (no Python in
    # the path) and raise the level so it's rarely invoked.
    try:
        _L.restore_default_callback()
        _L.set_level(_L.ERROR)
    except Exception:
        pass
