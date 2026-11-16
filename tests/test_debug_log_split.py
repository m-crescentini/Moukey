"""The debug log is split: the mouse page and the keyboard page each show only
their own traffic, and keyboard key presses are logged."""

import copy
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from core.config import DEFAULT_CONFIG
from core.debug_log import KEYBOARD, MOUSE, debug_message_source
from core.device_hook_base import BaseDeviceHook
from core.device_hook_types import DeviceEvent
from core.logi_device_catalog import KEYBOARD_KEY_CIDS
from ui.backend import Backend


ROOT = Path(__file__).resolve().parents[1]


class SourceClassificationTests(unittest.TestCase):
    def test_keyboard_messages(self):
        for message in (
            "Dispatch kbd_brightness_down callbacks=0",
            "No mapped action for kbd_mute",
            "Mapped kbd_volume_up -> play_pause (Play/Pause)",
            "Keyboard key kbd_mute pressed (diverted by Moukey)",
            "Keyboard keys diverted by Moukey: kbd_mute",
            "Keyboard backlight -> off",
            "Backlight re-enabled by key; back to passthrough",
            "Dispatch crown_left callbacks=1",
        ):
            with self.subTest(message=message):
                self.assertEqual(debug_message_source(message), KEYBOARD)

    def test_mouse_and_general_messages(self):
        for message in (
            "WM_MBUTTONDOWN  mouseData=0x00000000  hiword=0  flags=0x0000",
            "Dispatch xbutton1_down callbacks=1",
            "Button gesture armed owner=middle",
            "HID gesture button down",
            "Hook mappings refreshed: gesture=task_view",
            "Active profile -> chrome",
            "Debug mode enabled",
            "Latency Hook callback: n=100 avg=0.30 ms",
            "",
            None,
        ):
            with self.subTest(message=message):
                self.assertEqual(debug_message_source(message), MOUSE)


class BackendDebugLogSplitTests(unittest.TestCase):
    def _backend(self):
        with (
            patch("ui.backend.load_config", return_value=copy.deepcopy(DEFAULT_CONFIG)),
            patch("ui.backend.save_config"),
            patch("ui.backend.supports_login_startup", return_value=False),
        ):
            return Backend(engine=None)

    def test_lines_go_to_their_own_log(self):
        backend = self._backend()
        backend._append_debug_line("Dispatch xbutton1_down callbacks=1")
        backend._append_debug_line("Keyboard key kbd_mute pressed (diverted by Moukey)")

        self.assertIn("xbutton1_down", backend.debugLog)
        self.assertNotIn("kbd_mute", backend.debugLog)
        self.assertIn("kbd_mute", backend.keyboardDebugLog)
        self.assertNotIn("xbutton1_down", backend.keyboardDebugLog)

    def test_explicit_source_wins_over_the_text(self):
        backend = self._backend()
        backend._append_debug_line("Latency Hook callback: n=1", "keyboard")
        self.assertIn("Latency", backend.keyboardDebugLog)
        self.assertEqual(backend.debugLog, "")

    def test_each_log_keeps_its_last_200_lines(self):
        backend = self._backend()
        for i in range(250):
            backend._append_debug_line(f"mouse line {i}")
            backend._append_debug_line(f"kbd_mute line {i}")
        self.assertEqual(len(backend.debugLog.splitlines()), 200)
        self.assertEqual(len(backend.keyboardDebugLog.splitlines()), 200)
        self.assertIn("mouse line 249", backend.debugLog)
        self.assertNotIn("mouse line 0]", backend.debugLog)

    def test_clear_one_log_leaves_the_other(self):
        backend = self._backend()
        backend._append_debug_line("Dispatch middle_down callbacks=1")
        backend._append_debug_line("Dispatch kbd_mute callbacks=0")
        backend.clearDebugLogFor("keyboard")
        self.assertEqual(backend.keyboardDebugLog, "")
        self.assertIn("middle_down", backend.debugLog)
        backend._append_debug_line("Dispatch kbd_mute callbacks=0")
        backend.clearDebugLogFor("mouse")
        self.assertEqual(backend.debugLog, "")
        self.assertIn("kbd_mute", backend.keyboardDebugLog)

    def test_clear_all_empties_both(self):
        backend = self._backend()
        backend._append_debug_line("Dispatch middle_down callbacks=1")
        backend._append_debug_line("Dispatch kbd_mute callbacks=0")
        backend.clearDebugLog()
        self.assertEqual((backend.debugLog, backend.keyboardDebugLog), ("", ""))

    def test_change_signal_fires_for_both_logs(self):
        backend = self._backend()
        seen = []
        backend.debugLogChanged.connect(lambda: seen.append(1))
        backend._append_debug_line("Dispatch middle_down callbacks=1")
        backend._append_debug_line("Dispatch kbd_mute callbacks=0")
        backend.clearDebugLogFor("keyboard")
        self.assertEqual(len(seen), 3)

    def test_latency_summary_goes_to_the_requested_page(self):
        backend = self._backend()
        backend._engine = SimpleNamespace(
            latency_summary=lambda: ["Latency Hook callback: n=1"])
        backend.logLatencySummaryFor("keyboard")
        self.assertIn("Latency", backend.keyboardDebugLog)
        self.assertEqual(backend.debugLog, "")
        backend.logLatencySummary()
        self.assertIn("Latency", backend.debugLog)


class HookKeyboardLoggingTests(unittest.TestCase):
    def test_a_diverted_key_press_is_logged_and_dispatched(self):
        hook = BaseDeviceHook()
        hook.debug_mode = True
        hook._debug_callback = Mock()
        seen = []
        hook.register("kbd_mute", lambda event: seen.append(event.event_type))

        hook._on_hid_keyboard_key("kbd_mute")

        self.assertEqual(seen, ["kbd_mute"])
        messages = [c.args[0] for c in hook._debug_callback.call_args_list]
        self.assertIn("Keyboard key kbd_mute pressed (diverted by Moukey)", messages)
        self.assertTrue(all(debug_message_source(m) == KEYBOARD for m in messages))

    def test_nothing_is_logged_with_debug_off(self):
        hook = BaseDeviceHook()
        hook._debug_callback = Mock()
        hook._on_hid_keyboard_key("kbd_mute")
        hook._debug_callback.assert_not_called()


class _FakeHook(BaseDeviceHook):
    """Stands in for the platform hook inside the engine."""

    def start(self):
        pass

    def stop(self):
        pass

    def refresh_diverts(self):
        pass


class EngineKeyboardLoggingTests(unittest.TestCase):
    def _engine(self, **kbd_maps):
        from core.engine import Engine
        cfg = copy.deepcopy(DEFAULT_CONFIG)
        cfg["profiles"]["default"]["mappings"].update(kbd_maps)
        # Keep the patches for the whole test: reload_mappings() re-reads the
        # config, which must never come from the real user file.
        for target, value in (
            ("core.engine.DeviceHook", _FakeHook),
            ("core.engine.AppDetector", Mock()),
        ):
            patcher = patch(target, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = patch("core.engine.load_config", side_effect=lambda: copy.deepcopy(cfg))
        patcher.start()
        self.addCleanup(patcher.stop)
        engine = Engine()
        engine.set_debug_enabled(True)
        engine.messages = []
        engine.set_debug_callback(engine.messages.append)
        return engine

    def test_diverted_keys_are_named_in_the_log(self):
        engine = self._engine(kbd_mute="play_pause", kbd_volume_up="mute")
        engine.reload_mappings()
        lines = [m for m in engine.messages if m.startswith("Keyboard keys diverted")]
        self.assertTrue(lines)
        self.assertIn("kbd_mute", lines[-1])
        self.assertIn("kbd_volume_up", lines[-1])
        self.assertNotIn("kbd_brightness", lines[-1])

    def test_no_remapped_keys_says_everything_is_native(self):
        engine = self._engine()
        engine.reload_mappings()
        lines = [m for m in engine.messages if m.startswith("Keyboard keys diverted")]
        self.assertIn("none", lines[-1])
        self.assertIn("native", lines[-1])

    def test_no_keyboard_line_when_debug_is_off(self):
        engine = self._engine(kbd_mute="play_pause")
        engine.set_debug_enabled(False)
        engine.messages.clear()
        engine.reload_mappings()
        self.assertEqual(
            [m for m in engine.messages if "Keyboard keys diverted" in m], [])

    def test_backlight_change_is_logged(self):
        engine = self._engine()
        engine.hook._hid_gesture = SimpleNamespace(set_backlight=Mock())
        engine.set_backlight(False, 0)
        self.assertIn("Keyboard backlight -> off", engine.messages)

    def test_engine_to_backend_routing(self):
        """End to end: what the engine emits lands in the right page log."""
        engine = self._engine(kbd_mute="play_pause")
        with (
            patch("ui.backend.load_config", return_value=copy.deepcopy(DEFAULT_CONFIG)),
            patch("ui.backend.save_config"),
            patch("ui.backend.supports_login_startup", return_value=False),
        ):
            backend = Backend(engine=None)
        engine.set_debug_callback(backend._handleDebugMessage)
        engine.reload_mappings()
        engine._emit_debug("Dispatch xbutton1_down callbacks=1")
        self.assertIn("Keyboard keys diverted by Moukey: kbd_mute",
                      backend.keyboardDebugLog)
        self.assertNotIn("Keyboard keys", backend.debugLog)
        self.assertIn("xbutton1_down", backend.debugLog)
        self.assertNotIn("xbutton1_down", backend.keyboardDebugLog)


class DevicePageDebugPanelSourceTests(unittest.TestCase):
    PAGE = (ROOT / "ui" / "qml" / "DevicePage.qml").read_text(encoding="utf-8")

    def test_panel_reads_the_log_of_its_own_page(self):
        self.assertIn("backend.keyboardDebugLog", self.PAGE)
        self.assertIn("debugPageLog", self.PAGE)
        self.assertNotIn("backend.debugLog.length", self.PAGE)
        self.assertIn("backend.clearDebugLogFor(deviceFilter)", self.PAGE)
        self.assertIn("backend.logLatencySummaryFor(deviceFilter)", self.PAGE)

    def test_gesture_only_controls_are_hidden_on_the_keyboard_page(self):
        self.assertGreaterEqual(self.PAGE.count('visible: deviceFilter !== "keyboard"'), 3)

    def test_keyboard_placeholder_explains_the_limit(self):
        self.assertIn('s["device.debug_placeholder_keyboard"]', self.PAGE)


class LocaleTests(unittest.TestCase):
    def test_keyboard_placeholder_in_every_language(self):
        from ui.locale_manager import _TRANSLATIONS
        for lang, table in _TRANSLATIONS.items():
            with self.subTest(lang=lang):
                self.assertIn("device.debug_placeholder_keyboard", table)


if __name__ == "__main__":
    unittest.main()
