"""Input-latency measurements for debug mode.

Three stages are timed while debug mode is on:

* ``hook``   -- how long the low-level mouse hook callback ran. On Windows the
  whole system waits for it, so a slow callback is felt as cursor lag.
* ``queue``  -- how long an event waited between being captured and being
  handed to its callbacks.
* ``action`` -- how long the callbacks (the remapped action) took to run.

Each metric keeps a bounded window of recent samples, so memory stays flat no
matter how long debug mode is left on. :meth:`LatencyMonitor.record` returns
a message when a sample is slower than its threshold, and a periodic summary
line every ``summary_every`` samples, so the debug log shows numbers instead of
impressions.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field


@dataclass(frozen=True)
class MetricSpec:
    """Display name, slow threshold (ms) and summary cadence of one metric."""

    label: str
    slow_ms: float
    summary_every: int


DEFAULT_METRICS = {
    "hook": MetricSpec("Hook callback", 10.0, 5000),
    "queue": MetricSpec("Event queue wait", 30.0, 100),
    "action": MetricSpec("Action run", 100.0, 100),
}

DEFAULT_WINDOW = 512


@dataclass
class _Metric:
    spec: MetricSpec
    window: deque = field(default_factory=lambda: deque(maxlen=DEFAULT_WINDOW))
    count: int = 0
    slow_count: int = 0
    total_ms: float = 0.0
    max_ms: float = 0.0


def _percentile(sorted_values, fraction):
    if not sorted_values:
        return 0.0
    index = min(len(sorted_values) - 1, int(round(fraction * (len(sorted_values) - 1))))
    return sorted_values[index]


class LatencyMonitor:
    """Collects timing samples per metric. Each metric has one writer thread."""

    def __init__(self, metrics=None, window=DEFAULT_WINDOW):
        self._metrics = {
            name: _Metric(spec, deque(maxlen=max(1, int(window))))
            for name, spec in (metrics or DEFAULT_METRICS).items()
        }

    def reset(self):
        for metric in self._metrics.values():
            metric.window.clear()
            metric.count = 0
            metric.slow_count = 0
            metric.total_ms = 0.0
            metric.max_ms = 0.0

    def record(self, name, ms, detail=None):
        """Store one sample (milliseconds).

        Returns a list of log lines to show: one when the sample is slower than
        its threshold (``detail`` may be a callable building extra text, so
        nothing is formatted on the fast path), and a summary every
        ``summary_every`` samples. Unknown metric names are ignored.
        """
        metric = self._metrics.get(name)
        if metric is None:
            return []
        ms = float(ms)
        if ms < 0:
            ms = 0.0
        metric.count += 1
        metric.total_ms += ms
        if ms > metric.max_ms:
            metric.max_ms = ms
        metric.window.append(ms)

        lines = []
        if ms > metric.spec.slow_ms:
            metric.slow_count += 1
            extra = ""
            if detail is not None:
                try:
                    text = detail() if callable(detail) else str(detail)
                except Exception:
                    text = ""
                extra = f" ({text})" if text else ""
            lines.append(
                f"SLOW {metric.spec.label}: {ms:.1f} ms "
                f"(limit {metric.spec.slow_ms:.0f} ms){extra}"
            )
        if metric.count % metric.spec.summary_every == 0:
            lines.append(self._summary_line(metric))
        return lines

    def _summary_line(self, metric):
        samples = sorted(metric.window)
        avg = metric.total_ms / metric.count if metric.count else 0.0
        return (
            f"Latency {metric.spec.label}: n={metric.count} avg={avg:.2f} ms "
            f"p95={_percentile(samples, 0.95):.2f} ms max={metric.max_ms:.1f} ms "
            f"slow={metric.slow_count}"
        )

    def summary_lines(self):
        """One summary line per metric that has samples, or a single note."""
        lines = [self._summary_line(m) for m in self._metrics.values() if m.count]
        return lines or ["Latency: no samples yet (debug mode must be on while you use the device)"]

    def stats(self, name):
        """Return {count, avg_ms, p95_ms, max_ms, slow} for tests and the UI."""
        metric = self._metrics[name]
        samples = sorted(metric.window)
        return {
            "count": metric.count,
            "avg_ms": metric.total_ms / metric.count if metric.count else 0.0,
            "p95_ms": _percentile(samples, 0.95),
            "max_ms": metric.max_ms,
            "slow": metric.slow_count,
        }
