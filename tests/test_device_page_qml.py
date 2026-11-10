"""Source-level guards for the action pickers on the device page."""

from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
DEVICE_PAGE = (ROOT / "ui" / "qml" / "DevicePage.qml").read_text(encoding="utf-8")


class DevicePagePickerTests(unittest.TestCase):
    def test_swipe_lists_leave_out_the_actions_ring(self):
        # The ring needs the button held down; as a tap/swipe action it would
        # swallow the gesture and do nothing.
        self.assertIn('a.id !== "activate_actions_ring"', DEVICE_PAGE)
        editor = DEVICE_PAGE.split("Per-button Gesture Swipe editor", 1)[1]
        editor = editor.split("DPI Presets editor", 1)[0]
        self.assertGreaterEqual(editor.count("model: swipeActions"), 2)
        self.assertNotIn("model: backend.allActions", editor)

    def test_every_live_action_list_can_pick_an_application(self):
        editor = DEVICE_PAGE.split("Per-button Gesture Swipe editor", 1)[1]
        editor = editor.split("DPI Presets editor", 1)[0]
        # tap + the four swipe directions share two handlers in the editor
        self.assertGreaterEqual(editor.count('aid === "__open_app__"'), 2)
        for key in ("hscroll_left", "hscroll_right"):
            self.assertIn(
                'backend.pickAppForMapping(selectedProfile, "%s")' % key,
                DEVICE_PAGE,
            )


if __name__ == "__main__":
    unittest.main()
