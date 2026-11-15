"""Latency measurements (debug mode) and the Windows hook fast path."""

import copy
import time
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from core.device_hook_base import BaseDeviceHook
from core.device_hook_types import DeviceEvent
from core.latency import LatencyMonitor, MetricSpec


class LatencyMonitorTests(unittest.TestCase):
    def _monitor(self, **kw):
        return LatencyMonitor({"m": MetricSpec("Metric", 10.0, kw.get("every", 3))},
                              window=kw.get("window", 512))

    def test_fast_samples_produce_no_slow_line(self):
        mon = self._monitor(every=1000)
        self.assertEqual(mon.record("m", 1.0), [])

    def test_slow_sample_reports_limit_and_lazy_detail(self):
        mon = self._monitor(every=1000)
        calls = []
        lines = mon.record("m", 25.0, lambda: calls.append(1) or "WM_XBUTTONDOWN")
        self.assertEqual(len(lines), 1)
        self.assertIn("SLOW Metric: 25.0 ms", lines[0])
        self.assertIn("limit 10 ms", lines[0])
        self.assertIn("WM_XBUTTONDOWN", lines[0])
        # a fast sample must never evaluate the detail callable
        mon.record("m", 1.0, lambda: calls.append(1) or "x")
        self.assertEqual(calls, [1])

    def test_broken_detail_does_not_raise(self):
        mon = self._monitor(every=1000)

        def boom():
            raise RuntimeError("nope")

        lines = mon.record("m", 50.0, boom)
        self.assertIn("SLOW Metric", lines[0])

    def test_summary_every_n_samples(self):
        mon = self._monitor(every=3)
        self.assertEqual(mon.record("m", 1.0), [])
        self.assertEqual(mon.record("m", 2.0), [])
        lines = mon.record("m", 3.0)
        self.assertEqual(len(lines), 1)
        self.assertIn("n=3", lines[0])
        self.assertIn("avg=2.00 ms", lines[0])
        self.assertIn("max=3.0 ms", lines[0])

    def test_stats_percentile_and_counts(self):
        mon = self._monitor(every=10000)
        for value in range(1, 101):
            mon.record("m", float(value))
        stats = mon.stats("m")
        self.assertEqual(stats["count"], 100)
        self.assertAlmostEqual(stats["avg_ms"], 50.5)
        self.assertEqual(stats["max_ms"], 100.0)
        self.assertGreaterEqual(stats["p95_ms"], 94.0)
        self.assertEqual(stats["slow"], 90)

    def test_window_is_bounded(self):
        mon = self._monitor(every=10 ** 9, window=8)
        for value in range(1000):
            mon.record("m", float(value % 5))
        self.assertEqual(len(mon._metrics["m"].window), 8)
        self.assertEqual(mon.stats("m")["count"], 1000)

    def test_unknown_metric_and_negative_values_are_safe(self):
        mon = self._monitor(every=10 ** 9)
        self.assertEqual(mon.record("other", 5.0), [])
        mon.record("m", -4.0)
        self.assertEqual(mon.stats("m")["max_ms"], 0.0)

    def test_summary_lines_and_reset(self):
        mon = self._monitor(every=10 ** 9)
        self.assertIn("no samples", mon.summary_lines()[0])
        mon.record("m", 4.0)
        self.assertIn("Latency Metric", mon.summary_lines()[0])
        mon.reset()
        self.assertEqual(mon.stats("m")["count"], 0)
        self.assertIn("no samples", mon.summary_lines()[0])


class BaseHookDispatchTimingTests(unittest.TestCase):
    def _hook(self, debug):
        hook = BaseDeviceHook()
        hook.debug_mode = debug
        hook._debug_callback = Mock()
        return hook

    def test_dispatch_records_nothing_when_debug_is_off(self):
        hook = self._hook(False)
        hook.register("middle_down", lambda e: None)
        hook._dispatch(DeviceEvent(DeviceEvent.MIDDLE_DOWN))
        self.assertEqual(hook.latency.stats("queue")["count"], 0)
        self.assertEqual(hook.latency.stats("action")["count"], 0)

    def test_dispatch_records_queue_wait_and_action_time(self):
        hook = self._hook(True)
        hook.register("middle_down", lambda e: time.sleep(0.12))
        event = DeviceEvent(DeviceEvent.MIDDLE_DOWN)
        event.timestamp = time.time() - 0.2  # waited 200 ms in the queue
        hook._dispatch(event)

        self.assertEqual(hook.latency.stats("queue")["count"], 1)
        self.assertGreater(hook.latency.stats("queue")["max_ms"], 150)
        self.assertGreater(hook.latency.stats("action")["max_ms"], 100)
        messages = [c.args[0] for c in hook._debug_callback.call_args_list]
        self.assertTrue(any(m.startswith("SLOW Event queue wait") for m in messages))
        self.assertTrue(any(m.startswith("SLOW Action run") for m in messages))

    def test_unmapped_event_has_no_action_sample(self):
        hook = self._hook(True)
        hook._dispatch(DeviceEvent(DeviceEvent.MIDDLE_DOWN))
        self.assertEqual(hook.latency.stats("queue")["count"], 1)
        self.assertEqual(hook.latency.stats("action")["count"], 0)

    def test_a_failing_callback_still_gets_timed_and_does_not_break_dispatch(self):
        hook = self._hook(True)

        def boom(event):
            raise RuntimeError("x")

        seen = []
        hook.register("middle_down", boom)
        hook.register("middle_down", lambda e: seen.append(1))
        hook._dispatch(DeviceEvent(DeviceEvent.MIDDLE_DOWN))
        self.assertEqual(seen, [1])
        self.assertEqual(hook.latency.stats("action")["count"], 1)


class EngineAndBackendLatencyTests(unittest.TestCase):
    def test_engine_summary_comes_from_the_hook(self):
        from core.engine import Engine
        engine = Engine.__new__(Engine)
        engine.hook = SimpleNamespace(latency=LatencyMonitor())
        engine.hook.latency.record("hook", 3.0)
        self.assertIn("Hook callback", engine.latency_summary()[0])
        engine.hook = SimpleNamespace()
        self.assertEqual(engine.latency_summary(), [])

    def test_backend_writes_the_summary_into_the_debug_log(self):
        from core.config import DEFAULT_CONFIG
        from ui.backend import Backend
        engine = SimpleNamespace(latency_summary=lambda: ["Latency Hook callback: n=1"])
        with (
            patch("ui.backend.load_config", return_value=copy.deepcopy(DEFAULT_CONFIG)),
            patch("ui.backend.save_config"),
            patch("ui.backend.supports_login_startup", return_value=False),
        ):
            backend = Backend(engine=None)
        backend._engine = engine
        backend.logLatencySummary()
        self.assertIn("Latency Hook callback: n=1", backend.debugLog)


if __name__ == "__main__":
    unittest.main()
