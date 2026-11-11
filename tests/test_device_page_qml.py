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


class MainPageLoaderTests(unittest.TestCase):
    """A page Loader that came up empty must be rebuilt instead of staying blank."""

    MAIN = (ROOT / "ui" / "qml" / "Main.qml").read_text(encoding="utf-8")

    def test_page_loaders_are_named_and_report_errors(self):
        for name in ("settingsLoader", "hapticLoader", "ringLoader"):
            self.assertIn("id: %s" % name, self.MAIN)
        self.assertGreaterEqual(self.MAIN.count("Loader.Error"), 4)

    def test_opening_a_page_retries_a_broken_loader(self):
        body = self.MAIN.split("function openPage(page)", 1)[1].split("color: theme.bg", 1)[0]
        self.assertGreaterEqual(body.count("reloadIfBroken(pageLoader(page))"), 2)


if __name__ == "__main__":
    unittest.main()
