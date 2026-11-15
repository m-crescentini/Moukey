"""The Windows low-level mouse hook skips the full handler for plain moves."""

import ctypes
import sys
import unittest
from unittest.mock import patch

from core.device_hook_types import DeviceEvent


@unittest.skipUnless(sys.platform == "win32", "Windows low-level hook")
class WindowsHookFastPathTests(unittest.TestCase):
    """WM_MOUSEMOVE must be passed straight through unless a button gesture is
    in progress; every other message keeps going through the full handler."""

    def setUp(self):
        import core.device_hook_windows as module
        self.module = module
        self.hook = module.DeviceHook()
        self.hook._hook = 1234
        patcher = patch.object(module, "CallNextHookEx", return_value=0)
        self.call_next = patcher.start()
        self.addCleanup(patcher.stop)
        self.struct = module.MSLLHOOKSTRUCT()
        self.lparam = ctypes.pointer(self.struct)

    def _call(self, wparam):
        return self.hook._low_level_handler(0, wparam, self.lparam)

    def test_plain_move_takes_the_fast_path(self):
        with patch.object(self.hook, "_low_level_handler_inner") as inner:
            self.assertEqual(self._call(self.module.WM_MOUSEMOVE), 0)
        inner.assert_not_called()
        self.call_next.assert_called_once()

    def test_fast_path_still_feeds_the_watchdog(self):
        self.hook._last_hook_event_at = 0.0
        self._call(self.module.WM_MOUSEMOVE)
        self.assertGreater(self.hook._last_hook_event_at, 0.0)

    def test_move_during_a_gesture_still_reaches_the_recognizer(self):
        self.hook._button_gesture_active_owner = "xbutton1"
        with patch.object(self.hook, "_low_level_handler_inner", return_value=7) as inner:
            self.assertEqual(self._call(self.module.WM_MOUSEMOVE), 7)
        inner.assert_called_once()

    def test_other_messages_always_use_the_full_handler(self):
        for wparam in (self.module.WM_MBUTTONDOWN, self.module.WM_XBUTTONUP,
                       self.module.WM_MOUSEWHEEL, self.module.WM_MOUSEHWHEEL):
            with self.subTest(wparam=hex(wparam)):
                with patch.object(self.hook, "_low_level_handler_inner",
                                  return_value=1) as inner:
                    self.assertEqual(self._call(wparam), 1)
                inner.assert_called_once()

    def test_blocked_button_is_still_swallowed(self):
        self.hook._should_intercept_events = lambda: True
        self.hook.block(DeviceEvent.MIDDLE_DOWN)
        self.assertEqual(self._call(self.module.WM_MBUTTONDOWN), 1)
        self.assertEqual(self.hook._dispatch_queue.get_nowait().event_type,
                         DeviceEvent.MIDDLE_DOWN)

    def test_debug_mode_times_the_callback_including_the_fast_path(self):
        self.hook.debug_mode = True
        self._call(self.module.WM_MOUSEMOVE)
        self.assertEqual(self.hook.latency.stats("hook")["count"], 1)

    def test_no_timing_when_debug_is_off(self):
        self._call(self.module.WM_MOUSEMOVE)
        self.assertEqual(self.hook.latency.stats("hook")["count"], 0)

    def test_a_crashing_inner_handler_still_passes_the_event_on(self):
        with patch.object(self.hook, "_low_level_handler_inner",
                          side_effect=RuntimeError("boom")):
            self.assertEqual(self._call(self.module.WM_MBUTTONDOWN), 0)
        self.call_next.assert_called()


if __name__ == "__main__":
    unittest.main()
