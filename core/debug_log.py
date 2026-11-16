"""Which debug log a message belongs to.

Debug mode writes everything the hook and the engine report into one stream.
The device pages show it split in two: the mouse page keeps pointer, button and
gesture traffic, the keyboard page keeps what concerns the keyboard (its
diverted keys, key presses, the crown and the backlight). A message belongs to
the keyboard log when it names one of the keyboard markers below; anything else
stays in the mouse log, which is also where general lines go.
"""

MOUSE = "mouse"
KEYBOARD = "keyboard"

# Substrings that only keyboard messages carry: the "kbd_*" button/event keys
# (kbd_brightness_down, ...), the Craft crown events and the keyboard/backlight
# wording used by the engine.
KEYBOARD_MARKERS = ("kbd_", "crown_", "Keyboard", "keyboard", "Backlight", "backlight")


def debug_message_source(message):
    """Return ``"keyboard"`` or ``"mouse"`` for a debug message."""
    text = str(message or "")
    if any(marker in text for marker in KEYBOARD_MARKERS):
        return KEYBOARD
    return MOUSE
