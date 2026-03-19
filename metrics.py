"""Lightweight in-memory metrics for bird observatory services.

Zero external dependencies (uses only stdlib + numpy which is already required).
Thread-safe. No disk I/O in hot path — numpy percentiles computed only on snapshot().

Usage:
    from metrics import MetricsRegistry
    m = MetricsRegistry()

    m.counter("frames_processed").inc()
    m.gauge("brightness").set(142.5)
    m.histogram("yolo_ms").record(31.2)
    m.funnel("pipeline", ["raw", "detected", "classified", "voted", "broadcast"])
    m.funnel("pipeline").inc("raw")

    snapshot = m.snapshot()  # JSON-serializable dict
"""

import resource
import threading
import time
from collections import deque

import numpy as np


class Counter:
    """Monotonically increasing counter."""

    __slots__ = ("_value", "_lock")

    def __init__(self):
        self._value = 0
        self._lock = threading.Lock()

    def inc(self, n=1):
        with self._lock:
            self._value += n

    @property
    def value(self):
        return self._value

    def snapshot(self):
        return self._value


class Gauge:
    """Current value gauge."""

    __slots__ = ("_value", "_lock")

    def __init__(self):
        self._value = 0.0
        self._lock = threading.Lock()

    def set(self, v):
        with self._lock:
            self._value = v

    @property
    def value(self):
        return self._value

    def snapshot(self):
        return self._value


class Histogram:
    """Rolling-window histogram with percentile computation.

    Keeps the last `max_samples` values in a deque. Percentiles are computed
    on demand via numpy (only during snapshot(), never in the hot path).
    """

    __slots__ = ("_values", "_lock", "_count", "_sum")

    def __init__(self, max_samples=1000):
        self._values = deque(maxlen=max_samples)
        self._lock = threading.Lock()
        self._count = 0
        self._sum = 0.0

    def record(self, value):
        with self._lock:
            self._values.append(value)
            self._count += 1
            self._sum += value

    def snapshot(self):
        with self._lock:
            if not self._values:
                return {
                    "count": 0, "sum": 0, "min": 0, "max": 0,
                    "p50": 0, "p95": 0, "p99": 0, "mean": 0,
                }
            arr = np.array(self._values)
        # numpy outside lock — snapshot() is called rarely (every 10-30s)
        p50, p95, p99 = np.percentile(arr, [50, 95, 99])
        return {
            "count": self._count,
            "sum": round(self._sum, 1),
            "min": round(float(arr.min()), 1),
            "max": round(float(arr.max()), 1),
            "p50": round(float(p50), 1),
            "p95": round(float(p95), 1),
            "p99": round(float(p99), 1),
            "mean": round(float(arr.mean()), 1),
        }


class Funnel:
    """Ordered set of counters that auto-compute drop rates between stages.

    Usage:
        f = Funnel(["raw", "detected", "classified", "broadcast"])
        f.inc("raw")
        f.inc("detected")
        # snapshot shows counts + drop % at each stage
    """

    def __init__(self, stages):
        self._stages = list(stages)
        self._counters = {s: 0 for s in stages}
        self._lock = threading.Lock()

    def inc(self, stage, n=1):
        with self._lock:
            self._counters[stage] = self._counters.get(stage, 0) + n

    def snapshot(self):
        with self._lock:
            counts = dict(self._counters)
        result = []
        prev = None
        for stage in self._stages:
            count = counts.get(stage, 0)
            if prev is not None and prev > 0:
                drop_pct = round((1 - count / prev) * 100, 1)
            else:
                drop_pct = 0.0
            result.append({
                "stage": stage,
                "count": count,
                "drop_pct": drop_pct,
            })
            prev = count
        return result


class MetricsRegistry:
    """Central registry for all metrics in a service.

    Thread-safe. Create one instance per service.
    """

    def __init__(self):
        self._counters = {}
        self._gauges = {}
        self._histograms = {}
        self._funnels = {}
        self._lock = threading.Lock()
        self._start_time = time.monotonic()

    def counter(self, name):
        with self._lock:
            if name not in self._counters:
                self._counters[name] = Counter()
            return self._counters[name]

    def gauge(self, name):
        with self._lock:
            if name not in self._gauges:
                self._gauges[name] = Gauge()
            return self._gauges[name]

    def histogram(self, name, max_samples=1000):
        with self._lock:
            if name not in self._histograms:
                self._histograms[name] = Histogram(max_samples)
            return self._histograms[name]

    def funnel(self, name, stages=None):
        with self._lock:
            if name not in self._funnels:
                if stages is None:
                    raise ValueError(f"Funnel '{name}' not found. Provide stages on first call.")
                self._funnels[name] = Funnel(stages)
            return self._funnels[name]

    def uptime_seconds(self):
        return time.monotonic() - self._start_time

    def update_resources(self):
        """Refresh rss_mb and uptime gauges from OS."""
        # macOS ru_maxrss is in bytes
        rss_bytes = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        self.gauge("rss_mb").set(round(rss_bytes / 1048576, 1))
        self.gauge("uptime_seconds").set(round(self.uptime_seconds(), 0))

    def snapshot(self):
        """Return a JSON-serializable dict of all metrics."""
        self.update_resources()
        result = {
            "uptime_seconds": round(self.uptime_seconds(), 0),
            "counters": {},
            "gauges": {},
            "histograms": {},
            "funnels": {},
        }
        with self._lock:
            counter_items = list(self._counters.items())
            gauge_items = list(self._gauges.items())
            histogram_items = list(self._histograms.items())
            funnel_items = list(self._funnels.items())

        for name, c in counter_items:
            result["counters"][name] = c.snapshot()
        for name, g in gauge_items:
            result["gauges"][name] = g.snapshot()
        for name, h in histogram_items:
            result["histograms"][name] = h.snapshot()
        for name, f in funnel_items:
            result["funnels"][name] = f.snapshot()

        return result
