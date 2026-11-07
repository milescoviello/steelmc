#!/usr/bin/env python3
"""SteelMC: one window for an Aerox 9 Wireless and an Apex Pro Gen 3, no GG needed.

The mouse goes through aerox.py. The keyboard goes through the apex-analog command, so its
safety checks stay in one place. Device calls run off the GUI thread, because a sleeping
mouse takes seconds to fail.
"""
import atexit
import base64
import json
import os
import queue
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading

from PySide6.QtCore import QEvent, QObject, QPoint, QProcess, QRect, QSize, Qt, QTimer, Signal
from PySide6.QtCore import QPointF
from PySide6.QtGui import QColor, QFont, QFontMetrics, QImage, QPainter, QPalette, QPen, QPolygonF
from PySide6.QtWidgets import (
    QAbstractButton, QApplication, QButtonGroup, QCheckBox, QColorDialog, QComboBox, QFrame, QGridLayout, QHBoxLayout,
    QInputDialog, QLabel, QLineEdit, QMainWindow, QMessageBox, QPushButton, QRadioButton, QRubberBand, QSizePolicy, QSlider,
    QSpinBox, QDoubleSpinBox, QTableWidget, QTableWidgetItem, QHeaderView, QAbstractItemView, QPlainTextEdit,
    QStackedWidget, QTabWidget, QVBoxLayout, QWidget,
)

sys.path.insert(0, os.path.join(os.path.dirname(os.path.realpath(__file__)), "..", "aerox"))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.realpath(__file__)), "..", "arctis"))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.realpath(__file__)), "..", "devices"))
import aerox  # noqa: E402
import arctis  # noqa: E402
import gear  # noqa: E402
import steelmc_devices  # noqa: E402

CHASSIS = "#26282b"
WELL = "#1b1d1f"
CAP = "#34373b"
CAP_HOVER = "#3e4146"
CAP_LIP = "#1f2123"
EDGE = "#0f1011"
LIGHT = "#4d5156"
TEXT = "#eceef0"
MUTED = "#a6abb1"
ACCENT = "#5480ff"
ACCENT_DIM = "#37509f"  # a key on its way down, before it fires
WARN = "#e3a53a"
BAD = "#f4806b"

STYLE = f"""
QWidget {{ color: {TEXT}; }}
QMainWindow, QTabWidget::pane, #page {{ background: {CHASSIS}; border: 0; }}
QTabBar {{ background: {EDGE}; }}
QTabBar::tab {{ background: {EDGE}; color: {MUTED}; padding: 9px 26px; border: 0; font-weight: 600; }}
QTabBar::tab:hover {{ color: {TEXT}; }}
QTabBar::tab:selected {{ background: {CHASSIS}; color: {TEXT}; border-top: 2px solid {ACCENT}; }}
QFrame#well {{
    background: {WELL};
    border: 1px solid {EDGE};
    border-bottom-color: {LIGHT};
    border-right-color: #3a3d41;
}}
QLabel#title {{ font-weight: 600; font-size: 11pt; }}
QLabel#muted {{ color: {MUTED}; }}
QLabel#status {{ color: {MUTED}; }}
QLabel#status[kind="warn"] {{ color: {WARN}; }}
QLabel#status[kind="bad"] {{ color: {BAD}; }}
QPushButton, QComboBox, QSpinBox {{
    background: {CAP};
    border: 1px solid {EDGE};
    border-top-color: {LIGHT};
    border-bottom: 2px solid {EDGE};
    padding: 4px 12px;
    min-height: 19px;
}}
QPushButton:hover, QComboBox:hover {{ background: {CAP_HOVER}; }}
QPushButton:pressed {{ background: {CAP_LIP}; border-top-color: {EDGE}; }}
QPushButton:focus, QComboBox:focus, QSpinBox:focus {{ border-color: {ACCENT}; }}
QPushButton#primary {{ background: #2f53c9; border-top-color: {ACCENT}; font-weight: 600; }}
QPushButton#primary:hover {{ background: #3a60dc; }}
QPushButton#segment:checked {{ background: {CAP_LIP}; border-color: {ACCENT}; border-bottom: 2px solid {ACCENT}; font-weight: 600; }}
QPushButton:disabled, QPushButton#primary:disabled, QComboBox:disabled, QSpinBox:disabled {{
    background: #2a2c2f; color: #71767c; border-top-color: #2a2c2f;
}}
QLineEdit {{ background: {EDGE}; border: 1px solid {EDGE}; border-bottom-color: {LIGHT}; padding: 4px 6px; min-height: 19px; }}
QLineEdit:focus {{ border-color: {ACCENT}; }}
QLineEdit:disabled {{ color: #71767c; }}
QComboBox QAbstractItemView {{ background: {WELL}; border: 1px solid {EDGE}; selection-background-color: #2f53c9; }}
QComboBox::drop-down {{ border: 0; width: 22px; }}
QComboBox::down-arrow {{ image: url(@@DOWN@@); width: 9px; height: 6px; }}
QComboBox::down-arrow:disabled {{ image: url(@@DOWN_OFF@@); }}
QSpinBox {{ padding-right: 24px; }}
QSpinBox::up-button, QSpinBox::down-button {{ width: 24px; border: 0; background: transparent; }}
QSpinBox::up-button:hover, QSpinBox::down-button:hover {{ background: {CAP_HOVER}; }}
QSpinBox::up-arrow {{ image: url(@@UP@@); width: 9px; height: 6px; }}
QSpinBox::down-arrow {{ image: url(@@DOWN@@); width: 9px; height: 6px; }}
QSpinBox::up-arrow:disabled {{ image: url(@@UP_OFF@@); }}
QSpinBox::down-arrow:disabled {{ image: url(@@DOWN_OFF@@); }}
QSlider::groove:horizontal {{ height: 6px; background: {EDGE}; border-bottom: 1px solid {LIGHT}; }}
QSlider::sub-page:horizontal {{ background: {ACCENT}; }}
QSlider::handle:horizontal {{
    width: 12px; margin: -6px 0; background: {CAP_HOVER};
    border: 1px solid {EDGE}; border-top-color: {LIGHT}; border-bottom: 2px solid {EDGE};
}}
QSlider::handle:horizontal:focus {{ border-color: {ACCENT}; }}
QSlider::sub-page:horizontal:disabled {{ background: {LIGHT}; }}
QSlider::handle:horizontal:disabled {{ background: #2a2c2f; }}
QCheckBox::indicator, QRadioButton::indicator {{
    width: 13px; height: 13px; background: {EDGE}; border: 1px solid {LIGHT};
}}
QRadioButton::indicator {{ border-radius: 7px; }}
QCheckBox::indicator:checked, QRadioButton::indicator:checked {{ background: {ACCENT}; border-color: {ACCENT}; }}
QCheckBox::indicator:checked:disabled, QRadioButton::indicator:checked:disabled {{ background: {LIGHT}; border-color: {LIGHT}; }}
QCheckBox:disabled, QRadioButton:disabled {{ color: #71767c; }}
QCheckBox:focus, QRadioButton:focus {{ color: {ACCENT}; }}
QToolTip {{ background: {WELL}; color: {TEXT}; border: 1px solid {LIGHT}; }}
"""

# Physical order on the side grid, from GG's button numbering: three across, four down.
SIDE_GRID = [["side 1", "side 2", "side 3"], ["side 4", "side 5", "side 6"],
             ["side 7", "side 8", "side 9"], ["side 10", "side 11", "side 12"]]
TOP_BUTTONS = ["left", "middle", "right", "tilt left", "tilt right", "dpi", "wheel up", "wheel down"]
BUTTON_LABELS = {
    "left": "Left", "right": "Right", "middle": "Wheel click", "tilt left": "Tilt left", "tilt right": "Tilt right",
    "dpi": "DPI button", "wheel up": "Wheel up", "wheel down": "Wheel down",
}
ACTION_KINDS = [("Keyboard keys", "keys"), ("Mouse button", "mouse"), ("Next DPI stage", "dpi"), ("Scroll up", "scroll-up"),
                ("Scroll down", "scroll-down"), ("Tilt left", "tilt-left"), ("Tilt right", "tilt-right"),
                ("Media key", "media"), ("Nothing", "none")]
KEY_PRESETS = ["alt+tab", "ctrl+c", "ctrl+v", "ctrl+x", "ctrl+z", "ctrl+shift+z", "ctrl+w", "ctrl+t", "ctrl+shift+t",
               "alt+f4", "meta", "esc", "enter", "printscreen", "f13", "f14", "f15"]
MEDIA_LABELS = {"play-pause": "Play or pause", "next": "Next track", "previous": "Previous track", "stop": "Stop",
                "mute": "Mute", "volume-up": "Volume up", "volume-down": "Volume down"}
TIMERS = [i * aerox.TIMER_STEP_MS for i in range(aerox.TIMER_MAX_MS // aerox.TIMER_STEP_MS + 1)]

# The keyboard's firmware levels, in millimetres. apex-analog picks the nearest for any depth.
DEPTHS = [0.1, 0.2, 0.4, 0.6, 0.8, 1.0, 1.2, 1.4, 1.6, 1.8, 2.0, 2.2, 2.4, 2.6, 2.8, 3.0, 3.2, 3.4, 3.7, 4.0]
TRAVEL_MM = 4.0
# How apex-analog spells keys that would otherwise break its own comma list or argument parser.
KEY_ARGS = {"`": "GRAVE", "-": "MINUS", "=": "EQUALS", "[": "LBRACKET", "]": "RBRACKET", "\\": "BACKSLASH",
            ";": "SEMICOLON", "'": "QUOTE", ",": "COMMA", ".": "PERIOD", "/": "SLASH"}
KEY_LABELS = {"BKSP": "Backspace", "TAB": "Tab", "CAPS": "Caps", "ENTER": "Enter", "LSHIFT": "Shift", "RSHIFT": "Shift",
              "LCTRL": "Ctrl", "RCTRL": "Ctrl", "LGUI": "Meta", "RGUI": "Meta", "LALT": "Alt", "RALT": "Alt",
              "SPACE": "Space", "ESC": "Esc", "PRTSC": "PrtSc", "SCRLK": "ScrLk", "PAUSE": "Pause", "INSERT": "Ins",
              "HOME": "Home", "PGUP": "PgUp", "DELETE": "Del", "END": "End", "PGDN": "PgDn", "UP": "Up", "DOWN": "Down",
              "LEFT": "Left", "RIGHT": "Right", "KP_NUM": "Num", "KP_DIV": "/", "KP_MUL": "*", "KP_SUB": "-", "KP_ADD": "+",
              "KP_ENTER": "Enter", "KP_DOT": ".", **{f"KP{n}": str(n) for n in range(10)}}
# An ANSI plate in key widths. None = a gap. These keys have no sensor unless `info` lists them.
PLATE = [
    [("ESC", 1), (None, 1), ("F1", 1), ("F2", 1), ("F3", 1), ("F4", 1), (None, .5), ("F5", 1), ("F6", 1), ("F7", 1), ("F8", 1),
     (None, .5), ("F9", 1), ("F10", 1), ("F11", 1), ("F12", 1), (None, .25), ("PRTSC", 1), ("SCRLK", 1), ("PAUSE", 1)],
    [*[(k, 1) for k in "`1234567890-="], ("BKSP", 2), (None, .25), ("INSERT", 1), ("HOME", 1), ("PGUP", 1)],
    [("TAB", 1.5), *[(k, 1) for k in "QWERTYUIOP[]"], ("\\", 1.5), (None, .25), ("DELETE", 1), ("END", 1), ("PGDN", 1)],
    [("CAPS", 1.75), *[(k, 1) for k in "ASDFGHJKL;'"], ("ENTER", 2.25)],
    [("LSHIFT", 2.25), *[(k, 1) for k in "ZXCVBNM,./"], ("RSHIFT", 2.75), (None, 1.25), ("UP", 1)],
    [("LCTRL", 1.25), ("LGUI", 1.25), ("LALT", 1.25), ("SPACE", 6.25), ("RALT", 1.25), ("RGUI", 1.25), ("SS", 1.25),
     ("RCTRL", 1.25), (None, .25), ("LEFT", 1), ("DOWN", 1), ("RIGHT", 1)],
]
# Label, the command, and the default colour of a pressed key where the effect has one.
NUMPAD_X = 18.5
# The numpad, as (name, column, row, width, height), counting from the number row.
NUMPAD = [
    ("KP_NUM", 0, 0, 1, 1), ("KP_DIV", 1, 0, 1, 1), ("KP_MUL", 2, 0, 1, 1), ("KP_SUB", 3, 0, 1, 1),
    ("KP7", 0, 1, 1, 1), ("KP8", 1, 1, 1, 1), ("KP9", 2, 1, 1, 1), ("KP_ADD", 3, 1, 1, 2),
    ("KP4", 0, 2, 1, 1), ("KP5", 1, 2, 1, 1), ("KP6", 2, 2, 1, 1),
    ("KP1", 0, 3, 1, 1), ("KP2", 1, 3, 1, 1), ("KP3", 2, 3, 1, 1), ("KP_ENTER", 3, 3, 1, 2),
    ("KP0", 0, 4, 2, 1), ("KP_DOT", 2, 4, 1, 1),
]
PLATE_WIDTH = NUMPAD_X + 4
PLATE_HEIGHT = 6.25


def plate_layout():
    """Every key as (name, x, y, width, height), in key widths. The F row sits a quarter key above the rest."""
    keys = []
    y = 0.0
    for index, row_keys in enumerate(PLATE):
        x = 0.0
        for name, width in row_keys:
            if name is not None:
                keys.append((name, x, y, width, 1))
            x += width
        y += 1.25 if index == 0 else 1
    keys += [(name, NUMPAD_X + x, 1.25 + row_y, w, h) for name, x, row_y, w, h in NUMPAD]
    return keys


# What the screen can be pinned to. The daemon's own names, in the order its hotkey cycles them.
SCREEN_MODES = [("Automatic", "auto"), ("System stats", "idle"), ("Lyrics", "lyrics"), ("Local model stats", "qwen"), ("Game stats", "game")]
SCREEN_SOURCES = {"notify": "a message", "idle": "system stats", "lyrics": "lyrics", "qwen": "local model stats",
                  "game": "game stats", "video": "a video"}
SCREEN_WIDTH, SCREEN_HEIGHT = 128, 40
EFFECTS = [
    ("Your normal lighting", None, None),
    ("Keys glow as you press, over normal lighting", ["lights", "--keep"], "ffffff"),
    ("Keys glow as you press, on a dark board", ["lights"], "ff5a00"),
    ("Ripples from each press", ["lights", "--effect", "ripple"], None),
    ("Follow the music", ["lights", "--effect", "music"], None),
    ("CPU and memory on the F row", ["lights", "--effect", "stats"], None),
    ("Heatmap of what you press", ["lights", "--effect", "heatmap"], None),
]
# Keys the keyboard keeps for its own settings while its SteelSeries key is held. A shortcut on SS itself fires on a tap.
SHORTCUT_TAKEN = {"Q", "I", "O", "T", "LGUI"}
SHORTCUT_KINDS = [("Press keys", "keys", "ctrl+c"), ("Type text", "text", "hello"), ("Run a command", "command", "firefox"),
                  ("Volume up", "volume+", ""), ("Volume down", "volume-", ""), ("Brightness up", "brightness+", ""),
                  ("Brightness down", "brightness-", "")]
# Rebinding keys on the keyboard itself, through apex-analog bind.
BIND_ACTION_KINDS = [kind for kind in ACTION_KINDS if kind[1] != "dpi"]
BIND_LAYERS = [("Normally", "base"), ("While the SteelSeries key is held", "ss"), ("On a deeper press", "deep")]
OTHER_CONTROLS = [("Wheel up", "WHEEL_UP"), ("Wheel down", "WHEEL_DOWN"), ("Wheel press", "WHEEL_PRESS"),
                  ("Button under the wheel", "MEDIA_BUTTON")]
CONTROL_LABELS = {name: label for label, name in OTHER_CONTROLS}
CAP_MEDIA = {"play-pause": "Play", "next": "Next", "previous": "Prev", "stop": "Stop", "mute": "Mute", "volume-up": "Vol+",
             "volume-down": "Vol-"}
FEATURES = [
    ("oled", "Key depth on the keyboard's screen", "Shows how far each key is pressed on the small display."),
]
# What a key can do on the virtual controller: (label, apex-analog map output, short label for the keycap). Sticks and
# triggers follow how far the key is pressed; buttons press at the key's actuation point.
CONTROLLER_OUTPUTS = [
    ("Left stick up", "ly-", "LS\u2191"), ("Left stick down", "ly+", "LS\u2193"), ("Left stick left", "lx-", "LS\u2190"),
    ("Left stick right", "lx+", "LS\u2192"), ("Right stick up", "ry-", "RS\u2191"), ("Right stick down", "ry+", "RS\u2193"),
    ("Right stick left", "rx-", "RS\u2190"), ("Right stick right", "rx+", "RS\u2192"), ("Left trigger", "lt", "LT"),
    ("Right trigger", "rt", "RT"), ("A", "a", "A"), ("B", "b", "B"), ("X", "x", "X"), ("Y", "y", "Y"), ("Left bumper", "lb", "LB"),
    ("Right bumper", "rb", "RB"), ("Left stick click", "ls", "L3"), ("Right stick click", "rs", "R3"), ("Start", "start", "Start"),
    ("Back", "back", "Back"), ("Guide", "guide", "Home"), ("D-pad up", "dpad-up", "D\u2191"), ("D-pad down", "dpad-down", "D\u2193"),
    ("D-pad left", "dpad-left", "D\u2190"), ("D-pad right", "dpad-right", "D\u2192"),
]
OWN_AXIS = "own axis"  # the picker's entry for giving each picked key the next free plain axis
CONTROLLER_SHORT = {out: short for _, out, short in CONTROLLER_OUTPUTS}
AXIS_LIMIT = 64  # apex-analog's plain axes: axis:1 to axis:64, eight to a virtual device
CONTROLLER_LAYOUTS = {
    "W A S D as the left stick": {"W": "ly-", "S": "ly+", "A": "lx-", "D": "lx+"},
    "W A S D for driving": {"W": "rt", "S": "lt", "A": "lx-", "D": "lx+"},
    "Every key its own axis": "every",
}
CONTROLLER_DEFAULTS = {"keys": {}, "toggle": None, "toggle_on": "00ff00", "toggle_off": "ff0000", "deadzone": 0.3, "full": 3.5, "curve": 1.0,
                       "type_too": False}
TOGGLE_KEYS = [c for c in "ABCDEFGHJKLMNPRSUVWXYZ"] + ["1", "2", "3", "4", "5", "6", "7", "8", "9", "0"]  # not SS+Q, I, O or T


AXIS_RULE = re.compile(r"axis:(\d+)([+-]?)")


def axis_of(out):
    """(number, half) for a plain axis output: half is "" for the whole axis, + or - for one of two keys sharing it."""
    match = AXIS_RULE.fullmatch(out) if isinstance(out, str) else None
    return (int(match.group(1)), match.group(2)) if match else None


def controller_short(out):
    axis = axis_of(out)
    if axis:
        return f"Ax{axis[0]}" + {"+": "\u2191", "-": "\u2193", "": ""}[axis[1]]
    return CONTROLLER_SHORT.get(out, out)


def controller_label(out):
    axis = axis_of(out)
    if axis:
        return f"Axis {axis[0]}" + {"+": ", top half", "-": ", bottom half", "": ""}[axis[1]]
    return dict((o, l) for l, o, _ in CONTROLLER_OUTPUTS).get(out, out)


def valid_controller_output(out):
    axis = axis_of(out)
    return out in CONTROLLER_SHORT or bool(axis and 1 <= axis[0] <= AXIS_LIMIT)


def next_axes(taken, count):
    """The lowest free axis numbers."""
    used = {axis_of(o)[0] for o in taken if axis_of(o)}
    free = [n for n in range(1, AXIS_LIMIT + 1) if n not in used]
    if len(free) < count:
        raise ValueError(f"Only {AXIS_LIMIT} axes: {len(free)} are free.")
    return [f"axis:{n}" for n in free[:count]]


ARG_KEYS = {v: k for k, v in KEY_ARGS.items()}


def controller_rules(mapping):
    """The setup as apex-analog map rules, one per line."""
    return "\n".join(f"{KEY_ARGS.get(k, k)}={out}" for k, out in sorted(mapping.items()))


def parse_controller_rules(text, keys):
    """KEY=OUTPUT lines back into a setup. Raises ValueError naming the first line that's wrong."""
    mapping = {}
    for number, line in enumerate(text.splitlines(), 1):
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        name, sep, out = line[1:].partition("=")
        name, out = (line[0] + name).strip(), out.strip()
        key = ARG_KEYS.get(name.upper(), ARG_KEYS.get(name, name.upper()))
        if not sep or key not in keys:
            raise ValueError(f"Line {number}: {line!r} should be KEY=OUTPUT, with a key that has a sensor.")
        if not valid_controller_output(out):
            raise ValueError(f"Line {number}: {out!r} isn't a controller output. Use ly-, rt, a, dpad-up, axis:3, axis:3+ ...")
        mapping[key] = out
    shared = {axis_of(o)[0] for o in mapping.values() if axis_of(o) and axis_of(o)[1]}
    whole = {axis_of(o)[0] for o in mapping.values() if axis_of(o) and not axis_of(o)[1]}
    if shared & whole:
        raise ValueError(f"Axis {min(shared & whole)} is used whole and as halves. Use axis:N on its own, or axis:N+ and axis:N-.")
    return mapping


def pretty_action(action):
    """`alt+tab` -> `Alt+Tab`, `mouse6` -> `Button 6`, for showing on a keycap."""
    if action == "none":
        return "Nothing"
    if action == "dpi":
        return "Next DPI"
    if action.startswith("mouse"):
        return f"Button {action[5:]}"
    if action.startswith("media:"):
        return MEDIA_LABELS.get(action[6:], action[6:])
    if action in ("scroll-up", "scroll-down", "tilt-left", "tilt-right"):
        return action.replace("-", " ").capitalize()
    if "+" not in action:
        return f"Key {action.upper() if len(action) == 1 else action.capitalize()}"
    return "+".join(part.upper() if len(part) == 1 else part.capitalize() for part in action.split("+"))


def shortcut_label(key):
    """SteelSeries + J, or for the SteelSeries key itself, a tap of it."""
    return "Tap the SteelSeries key alone" if key == "SS" else f"SteelSeries + {KEY_LABELS.get(key, key)}"


def binding_label(entry, layer):
    """What to print on a keycap for its binding on this layer. Empty when it does what's printed on it."""
    action = entry["action"]
    if entry["default"] and (layer == "base" or action == "none"):
        return ""
    if action.startswith("builtin:"):
        return "Fn"  # one of the keyboard's own functions, as GG calls this layer
    if action.startswith("raw:"):
        return "Custom"
    if action.startswith("media:"):
        return CAP_MEDIA.get(action[6:], action[6:])
    label = pretty_action(action)
    return label[4:] if label.startswith("Key ") else label


def binding_changes(before, after):
    """[(key, action, layer)] that puts `after` back to `before`, both as `apex-analog bindings --json` gives them."""
    items = []
    for layer, entries in before["layers"].items():
        for key, entry in entries.items():
            if after["layers"].get(layer, {}).get(key, {}).get("raw") != entry["raw"] and not entry["action"].startswith("raw:"):
                items.append((key, entry["action"], layer))
    for layer, entries in before.get("wheel", {}).items():
        for key, entry in entries.items():
            if after.get("wheel", {}).get(layer, {}).get(key, {}).get("raw") != entry["raw"] and not entry["action"].startswith("raw:"):
                items.append((key, entry["action"], layer))
    return items


def timer_label(ms):
    return aerox.format_timer(ms).replace("off", "Off")


class Runner(QObject):
    """Runs device calls one at a time off the GUI thread, and hands results back on it."""

    _finished = Signal(object, object)

    def __init__(self):
        super().__init__()
        self._jobs = queue.Queue()
        self._finished.connect(lambda callback, value: callback(value))
        threading.Thread(target=self._loop, daemon=True).start()

    def submit(self, call, done=None, failed=None):
        self._jobs.put((call, done, failed))

    def _loop(self):
        while True:
            call, done, failed = self._jobs.get()
            try:
                value = call()
            except (aerox.AeroxError, arctis.ArctisError, gear.GearError, KeyboardError, OSError) as e:
                if failed:
                    self._finished.emit(failed, e)
            else:
                if done:
                    self._finished.emit(done, value)


class MouseDevice:
    """Opens the mouse for each call, so `aerox` in a terminal can use it in between."""

    def read(self):
        with aerox.Aerox.open() as m:
            m.ready()
            return {
                "connection": m.kind,
                "firmware": m.string(0xD0),
                "battery": m.battery(),
                "polling": m.polling(),
                "dpi": m.dpi(),
                "sleep_ms": m.sleep_ms(),
                "low_power": m.low_power(),
                "lighting": m.lighting(),
                "reactive": m.reactive(),
                "buttons": m.buttons(),
                "backup": m.backup(),
            }

    def battery(self):
        with aerox.Aerox.open() as m:
            m.ready()
            return m.battery()

    def call(self, method, *args, **kwargs):
        with aerox.Aerox.open() as m:
            m.ready()
            return getattr(m, method)(*args, **kwargs)

    def undo(self, backup):
        with aerox.Aerox.open() as m:
            m.ready()
            m.restore(backup)
            m.clear_zone_colors()


class HeadsetDevice:
    """Opens the transmitter for each call, so `arctis` in a terminal can use it in between."""

    def read(self):
        with arctis.Arctis.open() as h:
            h.ready()
            return {"status": h.status(), "versions": h.versions(), "sidetone": h.sidetone(), "mic": h.mic(),
                    "surround": h.surround(), "preset": h.eq_preset(), "bands": h.eq_bands(), "sleep": h.sleep_seconds()}

    def status(self):
        with arctis.Arctis.open() as h:
            return h.status()

    def call(self, method, *args):
        with arctis.Arctis.open() as h:
            h.ready()
            return getattr(h, method)(*args)


class KeyboardError(Exception):
    """A message from apex-analog worth showing as it is."""


class KeyboardDevice:
    """Drives the apex-analog command. The tool does its own checks and read-backs."""

    def __init__(self, exe="apex-analog"):
        self.exe = exe

    def run(self, *args, timeout=25):
        if not shutil.which(self.exe):
            raise KeyboardError("apex-analog isn't installed, so the keyboard can't be reached.")
        try:
            done = subprocess.run([self.exe, *args], capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL)
        except subprocess.TimeoutExpired:
            raise KeyboardError(f"apex-analog {args[0]} didn't finish in {timeout} s.") from None
        if done.returncode != 0:
            lines = (done.stderr or done.stdout).strip().splitlines()
            raise KeyboardError(lines[-1] if lines else f"apex-analog {args[0]} failed.")
        return done.stdout

    def stream_command(self):
        return [self.exe, "stream", "--hz", "60"]

    def read(self):
        info = json.loads(self.run("info", "--json"))
        status = self.run("status")
        presets = [line.strip() for line in self.run("preset", "list").splitlines() if line.strip()]
        return {"info": info, "jobs": parse_jobs(status), "presets": presets, "shortcuts": self.shortcuts(), "bindings": self.bindings(),
                "controller": self.controller()}

    def bindings(self):
        try:
            return json.loads(self.run("bindings", "--json"))
        except (KeyboardError, ValueError):  # an apex-analog from before bindings
            return None

    def bind(self, keys, action, layer):
        for key in keys:
            self.run("bind", KEY_ARGS.get(key, key), action, "--layer", layer)

    def sync_ss_lights(self):
        """Light up every key bound on the SteelSeries layer while SS is held, the way GG does."""
        return self.run("ss-lights", "sync")

    def restore_bindings(self, items):
        """Put keys back as they were: [(key, action, layer)]."""
        for key, action, layer in items:
            self.run("bind", KEY_ARGS.get(key, key), action, "--layer", layer)

    def shortcuts(self):
        try:
            return json.loads(self.run("shortcut", "list", "--json"))
        except (KeyboardError, ValueError):  # an apex-analog from before shortcuts
            return {}

    def add_shortcut(self, key, kind, value):
        """Keys and text are saved as a macro named after the key, so editing one replaces it."""
        name = KEY_ARGS.get(key, key)
        if kind in ("keys", "text"):
            macro = f"ss-{name.lower()}"
            step = value.upper() if kind == "keys" else f"text:{value}"
            self.run("macro", "add", macro, step)
            output = f"macro:{macro}"
        elif kind == "command":
            output = f"run:{value}"
        else:
            output = kind
        return self.run("shortcut", "add", name, output)

    def remove_shortcut(self, key):
        return self.run("shortcut", "remove", KEY_ARGS.get(key, key))

    @staticmethod
    def key_args(names):
        return ",".join(KEY_ARGS.get(n, n) for n in names)

    def set_actuation(self, names, mm):
        return self.run("actuation", self.key_args(names), f"{mm:g}")

    def set_rapid(self, names, on):
        return self.run("rapid", self.key_args(names), "on" if on else "off")

    def set_deep(self, names, mm):
        """Where a deeper press starts. 0 puts it back at the bottom."""
        return self.run("actuation", self.key_args(names), f"{mm:g}", "--deep")

    def lights(self, args, color=None):
        if args is None:
            return self.run("lights", "--off")
        return self.run(*args, *(["--color", color] if color else []))

    @staticmethod
    def controller_path():
        base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
        return os.path.join(base, "steelmc", "controller.json")

    def controller(self):
        """The virtual controller setup kept on this computer: which keys do what, the SS toggle, and how presses map to axes."""
        config = dict(CONTROLLER_DEFAULTS, keys={})
        try:
            with open(self.controller_path()) as f:
                data = json.load(f)
        except (OSError, ValueError):
            return config
        if isinstance(data, dict):
            config.update({k: v for k, v in data.items() if k in CONTROLLER_DEFAULTS})
            config["keys"] = {k: v for k, v in (data.get("keys") or {}).items() if valid_controller_output(v)}
        return config

    @staticmethod
    def controller_args(config):
        argv = ["map", *[f"{KEY_ARGS.get(k, k)}={out}" for k, out in sorted(config["keys"].items())],
                "--deadzone", f"{config['deadzone']:g}", "--full", f"{config['full']:g}", "--curve", f"{config['curve']:g}"]
        if config.get("toggle"):
            argv += ["--toggle", config["toggle"], "--toggle-colors", f"{config['toggle_on']},{config['toggle_off']}"]
        if config.get("type_too"):
            argv.append("--no-swallow")
        return argv

    def set_controller(self, config, run=True):
        """Keep the setup, and run it (or stop it when no key is set). Those keys stop typing while it runs."""
        path = self.controller_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path + ".tmp", "w") as f:
            json.dump(config, f, indent=1)
        os.replace(path + ".tmp", path)
        if config["keys"] and run:
            return self.run(*self.controller_args(config))
        return self.run("off", "map")

    def start(self, name):
        return self.run(name)

    def stop(self, name):
        return self.run("off", name)

    def use_preset(self, name):
        return self.run("preset", "use", name)

    def save_preset(self, name):
        return self.run("preset", "save", name)

    def delete_preset(self, name):
        return self.run("preset", "delete", name)

    def save(self):
        return self.run("save", "--yes", timeout=40)

    def report(self, name):
        """A report's summary: everything up to its first blank line."""
        return self.run(name, timeout=40).strip().split("\n\n")[0]


class DepthStream(QObject):
    """Live key depths from `apex-analog stream`. Runs only while something is watching."""

    changed = Signal(dict)
    RETRY_MS = 3000

    def __init__(self, command, parent=None):
        super().__init__(parent)
        self.command = command
        self.process = None
        self.wanted = False
        self.buffer = b""
        self.last = {}

    def start(self):
        self.wanted = True
        if self.process or not shutil.which(self.command[0]):
            return
        self.buffer = b""
        process = QProcess(self)
        process.setStandardErrorFile(QProcess.nullDevice())
        process.readyReadStandardOutput.connect(self._read)
        process.finished.connect(self._finished)
        process.start(self.command[0], self.command[1:])
        self.process = process

    def stop(self):
        self.wanted = False
        process, self.process = self.process, None
        if process:
            process.readyReadStandardOutput.disconnect(self._read)
            process.finished.disconnect(self._finished)
            process.kill()
            process.waitForFinished(500)
            process.deleteLater()
        self._publish({})

    def _read(self):
        if not self.process:  # stopping: killing the process can flush one last read
            return
        self.buffer += bytes(self.process.readAllStandardOutput())
        *lines, self.buffer = self.buffer.split(b"\n")
        if not lines:
            return
        try:  # only the newest frame matters: older ones are already out of date
            self._publish(json.loads(lines[-1]))
        except ValueError:
            pass

    def _publish(self, depths):
        if depths != self.last:
            self.last = depths
            self.changed.emit(depths)

    def _finished(self, *_):
        process, self.process = self.process, None
        if process:
            process.deleteLater()
        self._publish({})  # a dead stream mustn't leave keys looking held down
        if self.wanted:
            QTimer.singleShot(self.RETRY_MS, self._retry)

    def _retry(self):
        if self.wanted:
            self.start()


class OledDevice:
    """Talks to the apex-oledd daemon, which owns the keyboard's screen."""

    def __init__(self, path=None):
        runtime = os.environ.get("XDG_RUNTIME_DIR", "/tmp")
        self.path = path or os.path.join(runtime, "apex-oledd.sock")

    def send(self, message):
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(1.0)
            try:
                s.connect(self.path)
                s.sendall((json.dumps(message) + "\n").encode())
                data = b""
                while b"\n" not in data:
                    chunk = s.recv(4096)
                    if not chunk:
                        break
                    data += chunk
            except OSError:
                raise KeyboardError("apex-oledd isn't running, so the screen can't be reached.") from None
        try:
            return json.loads(data or b"{}")
        except ValueError:
            raise KeyboardError("apex-oledd sent a reply this app can't read.") from None

    def frame(self):
        """What's on the screen, and why. A daemon too old to say gets `outdated`."""
        reply = self.send({"cmd": "frame"})
        if reply.get("ok"):
            return reply
        status = self.send({"cmd": "status"})
        return {**status, "fb": None, "outdated": True}

    def set_mode(self, mode):
        return self.send({"cmd": "action", "action": f"mode:{mode}"})

    def toggle_power(self):
        return self.send({"cmd": "action", "action": "blank"})

    def show_message(self, text, secs=5):
        return self.send({"cmd": "notify", "lines": [text], "secs": secs, "align": "center"})


def oled_image(framebuffer):
    """Unpack the panel's format: 5 pages of 128 columns, one byte each, bit N = the pixel N rows down that page."""
    pixels = bytearray(SCREEN_WIDTH * SCREEN_HEIGHT)
    for page in range(SCREEN_HEIGHT // 8):
        for x in range(SCREEN_WIDTH):
            byte = framebuffer[page * SCREEN_WIDTH + x]
            if byte:
                for bit in range(8):
                    if byte >> bit & 1:
                        pixels[(page * 8 + bit) * SCREEN_WIDTH + x] = 255
    return QImage(bytes(pixels), SCREEN_WIDTH, SCREEN_HEIGHT, SCREEN_WIDTH, QImage.Format_Grayscale8).copy()


def parse_jobs(status):
    """Lines like `lights     on      lights --keep  (135 min)`."""
    jobs = []
    for line in status.splitlines():
        match = re.fullmatch(r"(\S+)\s+(on|off)\s+(.*?)\s+\((.*)\)", line.strip())
        if match:
            jobs.append({"name": match.group(1), "on": match.group(2) == "on", "command": match.group(3), "age": match.group(4)})
    return jobs


class KeyCap(QAbstractButton):
    """A keycap: machined edges and a front lip, with no glow. `fill` shades it from the top down."""

    def __init__(self, title, detail="", parent=None, inert=False):
        super().__init__(parent)
        self.title, self.detail = title, detail
        self.fill = None
        self.marked = False
        self.inert = inert
        self.live_mm = 0.0  # how far the key is pressed right now
        self.fires_at = None
        self.setCheckable(True)
        self.setEnabled(not inert)
        self.setFocusPolicy(Qt.NoFocus if inert else Qt.StrongFocus)
        self.setAttribute(Qt.WA_Hover)

    def set_detail(self, detail, fill=None, marked=False):
        self.detail, self.fill, self.marked = detail, fill, marked
        self.update()

    def set_live(self, mm):
        if mm != self.live_mm:
            self.live_mm = mm
            self.update()

    def fired(self):
        return self.fires_at is not None and self.live_mm >= self.fires_at

    def paintEvent(self, event):
        p = QPainter(self)
        r = self.rect()
        sunk = self.isChecked() or self.isDown() or self.fired()
        p.fillRect(r, QColor(EDGE))
        if self.inert:
            p.fillRect(r.adjusted(1, 1, -1, -1), QColor("#222427"))
            p.setPen(QColor("#6f7379"))
            small = QFont(self.font())
            small.setPointSizeF(self.font().pointSizeF() - 1.5)
            p.setFont(small)
            p.drawText(r.adjusted(6, 4, -3, -3), Qt.AlignLeft | Qt.AlignTop, self.title)
            return
        top = 3 if sunk else 1
        lip = 1 if sunk else 4
        face = QRect(r.left() + 1, r.top() + top, r.width() - 2, r.height() - top - lip)
        p.fillRect(r.adjusted(1, 1, -1, -1), QColor(CAP_LIP))
        hover = self.underMouse() and self.isEnabled()
        p.fillRect(face, QColor(CAP_HOVER if hover else CAP))
        if self.fill is not None and self.isEnabled():
            shade = QColor(ACCENT)
            shade.setAlpha(80)
            height = round(face.height() * max(0.0, min(1.0, self.fill)))
            p.fillRect(QRect(face.left(), face.top(), face.width(), height), shade)
            if self.live_mm:
                pressed = round(face.height() * min(1.0, self.live_mm / TRAVEL_MM))
                p.fillRect(QRect(face.left(), face.top(), face.width(), pressed), QColor(ACCENT if self.fired() else ACCENT_DIM))
            p.fillRect(QRect(face.left(), face.top() + height, face.width(), 1), QColor(TEXT if self.live_mm else ACCENT))
        if not sunk:
            p.fillRect(QRect(face.left(), face.top(), face.width(), 1), QColor(LIGHT))
        color = QColor(TEXT if self.isEnabled() else "#71767c")
        p.setPen(color)
        heading = QFont(self.font())
        heading.setWeight(QFont.DemiBold)
        p.setFont(heading)
        inner = face.adjusted(7, 4, -5, -3)
        p.drawText(inner, Qt.AlignLeft | Qt.AlignTop, self.title)
        detail = f"{self.live_mm:.1f}" if self.live_mm else self.detail
        if detail:
            small = QFont(self.font())
            small.setPointSizeF(self.font().pointSizeF() - 1)
            p.setFont(small)
            p.setPen(QColor(TEXT if self.marked or self.live_mm else MUTED) if self.isEnabled() else color)
            text = p.fontMetrics().elidedText(detail, Qt.ElideRight, inner.width())
            p.drawText(inner, Qt.AlignLeft | Qt.AlignBottom, text)
        if self.marked and self.isEnabled():
            p.fillRect(QRect(face.right() - 8, face.top() + 5, 4, 4), QColor(WARN))
        if self.isChecked():
            p.setPen(QPen(QColor(ACCENT if self.isEnabled() else LIGHT), 2))
            p.drawRect(r.adjusted(1, 1, -2, -2))
        elif self.hasFocus():
            p.setPen(QPen(QColor(ACCENT), 1))
            p.drawRect(r.adjusted(0, 0, -1, -1))


class ActionPicker(QWidget):
    """What a button or key should do: a kind, then the details for it. Takes and gives the text aerox and apex-analog parse."""

    def __init__(self, kinds, parent=None):
        super().__init__(parent)
        self.kind = QComboBox()
        for label, value in kinds:
            self.kind.addItem(label, value)
        self.kind.currentIndexChanged.connect(self.show_kind)
        self.keys_edit = QComboBox()
        self.keys_edit.setEditable(True)
        self.keys_edit.setInsertPolicy(QComboBox.NoInsert)
        self.keys_edit.addItems(KEY_PRESETS)
        self.keys_edit.lineEdit().setPlaceholderText("alt+tab")
        self.mouse_number = QComboBox()
        for n in range(1, 9):
            self.mouse_number.addItem(f"Button {n}", f"mouse{n}")
        self.media_key = QComboBox()
        for value, label in MEDIA_LABELS.items():
            self.media_key.addItem(label, f"media:{value}")
        self.kind_fields = QStackedWidget()
        for widget in (self.keys_edit, self.mouse_number, self.media_key, QWidget()):
            self.kind_fields.addWidget(widget)
        self.kind_fields.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.kind_fields.setFixedHeight(32)
        layout = row(self.kind, self.kind_fields)
        layout.setContentsMargins(0, 0, 0, 0)
        self.setLayout(layout)

    def set_action(self, action):
        if action.startswith("mouse"):
            kind = "mouse"
            self.mouse_number.setCurrentIndex(self.mouse_number.findData(action))
        elif action.startswith("media:"):
            kind = "media"
            self.media_key.setCurrentIndex(max(0, self.media_key.findData(action)))
        elif self.kind.findData(action) >= 0:
            kind = action
        else:
            kind = "keys"
            self.keys_edit.setCurrentText(action)
        self.kind.setCurrentIndex(self.kind.findData(kind))
        self.show_kind()

    def show_kind(self, *_):
        self.kind_fields.setCurrentIndex({"keys": 0, "mouse": 1, "media": 2}.get(self.kind.currentData(), 3))

    def action_text(self):
        kind = self.kind.currentData()
        if kind == "keys":
            return self.keys_edit.currentText().strip()
        if kind == "mouse":
            return self.mouse_number.currentData()
        if kind == "media":
            return self.media_key.currentData()
        return kind


class OledView(QWidget):
    """What's on the keyboard's screen, sharp at a whole-number zoom."""

    def __init__(self, zoom=1, parent=None):
        super().__init__(parent)
        self.zoom = zoom
        self.image = None

    def set_image(self, image):
        self.image = image
        self.update()

    def paintEvent(self, event):
        p = QPainter(self)
        p.fillRect(self.rect(), QColor("#000000"))
        if self.image is not None:
            width, height = SCREEN_WIDTH * self.zoom, SCREEN_HEIGHT * self.zoom
            p.drawImage(QRect((self.width() - width) // 2, (self.height() - height) // 2, width, height), self.image)


class WheelPart(QWidget):
    """The volume wheel, with the ridges of the real one."""

    def paintEvent(self, event):
        p = QPainter(self)
        r = self.rect()
        p.fillRect(r, QColor(EDGE))
        inner = r.adjusted(2, 1, -2, -1)
        p.fillRect(inner, QColor("#2b2d30"))
        for y in range(inner.top() + 1, inner.bottom(), 3):
            p.fillRect(QRect(inner.left(), y, inner.width(), 1), QColor(LIGHT))


class MediaButtonPart(QWidget):
    """The button under the wheel, with the bar printed on the real one."""

    def paintEvent(self, event):
        p = QPainter(self)
        r = self.rect()
        p.fillRect(r, QColor(EDGE))
        p.fillRect(r.adjusted(1, 1, -1, -2), QColor(CAP))
        p.fillRect(QRect(r.width() // 2 - 8, r.height() // 2 - 1, 16, 2), QColor(MUTED))


class KeyPlate(QWidget):
    """The whole keyboard, to scale. Click a key to pick it, drag across keys to pick them all,
    or drag from an empty spot to box them."""

    picked = Signal()
    STEP = 6  # pixels between checks along a drag, so a fast one can't jump over a key

    def __init__(self, unit, parent=None):
        super().__init__(parent)
        self.caps = {}
        self.drag_mode = None
        self.paint_to = True
        self.drag_from = self.drag_last = QPoint()
        self.before = set()
        self.band = QRubberBand(QRubberBand.Rectangle, self)

        def area(x, y, w, h):
            return QRect(round(x * unit), round(y * unit), round(w * unit) - 3, round(h * unit) - 3)

        for name, x, y, w, h in plate_layout():
            cap = KeyCap(KEY_LABELS.get(name, name), parent=self, inert=True)
            cap.key_name = name
            cap.setGeometry(area(x, y, w, h))
            cap.installEventFilter(self)
            cap.clicked.connect(lambda *_: self.picked.emit())  # Space on a focused key
            self.caps[name] = cap
        self.screen = OledView(1, self)
        self.screen.setGeometry(area(NUMPAD_X, 0, 3, 1))
        self.screen.setToolTip("The keyboard's screen, live")
        self.wheel = WheelPart(self)
        self.wheel.setGeometry(area(NUMPAD_X + 3, 0, 1, .58))
        self.wheel.setToolTip("Volume wheel: scroll for volume, press to mute")
        self.media = MediaButtonPart(self)
        self.media.setGeometry(area(NUMPAD_X + 3, .62, 1, .38))
        self.media.setToolTip("Play or pause")
        self.setFixedSize(round(PLATE_WIDTH * unit), round(PLATE_HEIGHT * unit))
        self.band.raise_()

    def cap_at(self, point):
        return next((c for c in self.caps.values() if not c.inert and c.geometry().contains(point)), None)

    def set_picked(self, cap, on):
        if cap.isChecked() != on:
            cap.setChecked(on)
            return True
        return False

    def begin_drag(self, point):
        cap = self.cap_at(point)
        if cap:
            self.drag_mode = "paint"
            self.paint_to = not cap.isChecked()  # starting on a picked key un-picks as you go
            self.drag_last = point
            self.set_picked(cap, self.paint_to)
            self.picked.emit()
        else:
            self.drag_mode = "box"
            self.drag_from = point
            self.before = {n for n, c in self.caps.items() if c.isChecked()}

    def continue_drag(self, point):
        changed = False
        if self.drag_mode == "paint":
            delta = point - self.drag_last
            steps = max(1, delta.manhattanLength() // self.STEP)
            for i in range(1, steps + 1):
                cap = self.cap_at(self.drag_last + delta * (i / steps))
                if cap:
                    changed = self.set_picked(cap, self.paint_to) or changed
            self.drag_last = point
        elif self.drag_mode == "box":
            box = QRect(self.drag_from, point).normalized()
            self.band.setGeometry(box)
            self.band.show()
            for name, cap in self.caps.items():
                if not cap.inert:
                    changed = self.set_picked(cap, name in self.before or cap.geometry().intersects(box)) or changed
        if changed:
            self.picked.emit()

    def end_drag(self):
        self.drag_mode = None
        self.band.hide()

    def eventFilter(self, cap, event):
        """A key takes the mouse when pressed, even a disabled one, so keys pass it up here and a drag can cross them."""
        kind = event.type()
        if kind in (QEvent.MouseButtonPress, QEvent.MouseButtonDblClick, QEvent.MouseMove, QEvent.MouseButtonRelease):
            point = cap.mapToParent(event.position().toPoint())
            if kind == QEvent.MouseMove:
                self.continue_drag(point)
            elif kind == QEvent.MouseButtonRelease:
                self.end_drag()
            elif event.button() == Qt.LeftButton:
                self.begin_drag(point)
            return True
        return False

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self.begin_drag(event.position().toPoint())

    def mouseMoveEvent(self, event):
        self.continue_drag(event.position().toPoint())

    def mouseReleaseEvent(self, event):
        self.end_drag()


class BatteryMeter(QWidget):
    """The mouse's 21 battery levels as a segmented gauge, with the same steps the firmware uses."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.level = 0
        self.charging = False
        self.setFixedSize(21 * 7 + 3, 18)

    def set_battery(self, battery):
        self.level, self.charging = battery["level"], battery["charging"]
        self.setToolTip(aerox.format_battery(battery))
        self.update()

    def clear(self):
        self.level, self.charging = 0, False
        self.setToolTip("")
        self.update()

    def paintEvent(self, event):
        p = QPainter(self)
        p.fillRect(self.rect(), QColor(EDGE))
        low = self.level <= 4 and not self.charging
        for i in range(21):
            cell = QRect(2 + i * 7, 2, 5, self.height() - 4)
            on = i < self.level
            p.fillRect(cell, QColor((WARN if low else ACCENT) if on else "#2a2c2f"))


class Swatch(QAbstractButton):
    """A colour you click to change."""

    picked = Signal(str)

    def __init__(self, color="ffffff", parent=None):
        super().__init__(parent)
        self.color = color
        self.setFixedSize(46, 24)

    def set_color(self, color):
        self.color = color
        self.update()

    def paintEvent(self, event):
        p = QPainter(self)
        p.fillRect(self.rect(), QColor(EDGE))
        p.fillRect(self.rect().adjusted(1, 1, -1, -1), QColor(LIGHT))
        p.fillRect(self.rect().adjusted(2, 2, -2, -2), QColor("#" + self.color) if self.isEnabled() else QColor("#2a2c2f"))
        if self.hasFocus():
            p.setPen(QPen(QColor(ACCENT), 2))
            p.drawRect(self.rect().adjusted(1, 1, -2, -2))

    def mouseReleaseEvent(self, event):
        super().mouseReleaseEvent(event)
        if self.rect().contains(event.position().toPoint()):
            self.choose()

    def choose(self):
        chosen = QColorDialog.getColor(QColor("#" + self.color), self, "Pick a colour")
        if chosen.isValid():
            self.set_color(chosen.name()[1:])
            self.picked.emit(self.color)


def well(*children, title=None, layout=None):
    """A recessed panel on the chassis."""
    frame = QFrame()
    frame.setObjectName("well")
    box = layout or QVBoxLayout()
    box.setContentsMargins(12, 10, 12, 12)
    box.setSpacing(8)
    frame.setLayout(box)
    if title:
        heading = QLabel(title)
        heading.setObjectName("title")
        if isinstance(box, QVBoxLayout):
            box.addWidget(heading)
    for child in children:
        if isinstance(box, QVBoxLayout):
            if isinstance(child, QWidget):
                box.addWidget(child)
            else:
                box.addLayout(child)
    return frame


def row(*items, stretch=True):
    box = QHBoxLayout()
    box.setSpacing(8)
    for item in items:
        if isinstance(item, QWidget):
            box.addWidget(item)
        elif item is None:
            box.addStretch(1)
        else:
            box.addLayout(item)
    return box


def header(*items):
    """A row that keeps its height whether or not Try again is showing, so nothing jumps."""
    strip = QWidget()
    layout = row(*items)
    layout.setContentsMargins(0, 0, 0, 0)
    strip.setLayout(layout)
    strip.setFixedHeight(34)
    return strip


def muted(text):
    label = QLabel(text)
    label.setObjectName("muted")
    return label


class StatusLine(QLabel):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("status")
        self.setWordWrap(True)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)

    def say(self, text, kind=""):
        self.setText(text)
        self.setProperty("kind", kind)
        self.style().unpolish(self)
        self.style().polish(self)


def ask(parent, title, text, confirm_label):
    """A yes or no question whose button says what will happen."""
    box = QMessageBox(parent)
    box.setWindowTitle(title)
    box.setText(text)
    yes = box.addButton(confirm_label, QMessageBox.AcceptRole)
    box.addButton("Cancel", QMessageBox.RejectRole)
    box.exec()
    return box.clickedButton() is yes


class MousePage(QWidget):
    def __init__(self, device, runner, confirm=ask, parent=None):
        super().__init__(parent)
        self.setObjectName("page")
        self.device, self.runner, self.confirm = device, runner, confirm
        self.loading = False
        self.connected = False
        self.dirty = False
        self.backup = None
        self.buttons_state = {}
        self.selected = None
        self.dpi_timer = QTimer(self, singleShot=True, interval=400)
        self.dpi_timer.timeout.connect(self.apply_dpi)
        self.light_timer = QTimer(self, singleShot=True, interval=300)
        self.light_timer.timeout.connect(self.apply_lighting)
        self.poller = QTimer(self, interval=20000)
        self.poller.timeout.connect(self.poll)
        self.build()

    def build(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(14, 12, 14, 12)
        root.setSpacing(10)

        self.connection = QLabel("Looking for the mouse")
        self.connection.setObjectName("title")
        self.firmware = muted("")
        self.meter = BatteryMeter()
        self.battery_label = QLabel("")
        self.retry = QPushButton("Try again")
        self.retry.clicked.connect(self.refresh)
        self.retry.hide()
        root.addWidget(header(self.connection, self.firmware, None, self.retry, self.battery_label, self.meter))

        self.content = QWidget()
        columns = QHBoxLayout(self.content)
        columns.setContentsMargins(0, 0, 0, 0)
        columns.setSpacing(10)
        columns.addLayout(self.build_buttons(), 5)
        columns.addLayout(self.build_right(), 4)
        root.addWidget(self.content, 1)

        self.status = StatusLine()
        self.undo_button = QPushButton("Undo changes")
        self.undo_button.clicked.connect(self.undo)
        self.save_button = QPushButton("Save to mouse")
        self.save_button.setObjectName("primary")
        self.save_button.clicked.connect(self.save)
        root.addLayout(row(self.status, self.undo_button, self.save_button))
        self.update_save_state()

    def build_buttons(self):
        self.caps = {}
        self.cap_group = QButtonGroup(self)
        self.cap_group.setExclusive(True)

        def cap(name, label):
            key = KeyCap(label)
            key.setMinimumSize(92, 58)
            key.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
            key.clicked.connect(lambda checked=False, n=name: self.select_button(n))
            self.caps[name] = key
            self.cap_group.addButton(key)
            return key

        side = QGridLayout()
        side.setSpacing(5)
        for y, cols in enumerate(SIDE_GRID):
            for x, name in enumerate(cols):
                side.addWidget(cap(name, name.split()[1]), y, x)
        top = QGridLayout()
        top.setSpacing(5)
        for i, name in enumerate(TOP_BUTTONS):
            top.addWidget(cap(name, BUTTON_LABELS[name]), i // 4, i % 4)
        grids = QVBoxLayout()
        grids.setSpacing(8)
        side_panel = well(muted("Side buttons"), side)
        top_panel = well(muted("Top and wheel"), top)
        grids.addWidget(side_panel, 3)
        grids.addWidget(top_panel, 2)
        grids.addWidget(self.build_editor())
        return grids

    def build_editor(self):
        self.editing = QLabel("Pick a button above")
        self.editing.setObjectName("title")
        self.picker = ActionPicker(ACTION_KINDS)
        self.kind, self.keys_edit = self.picker.kind, self.picker.keys_edit
        self.mouse_number, self.media_key = self.picker.mouse_number, self.picker.media_key
        self.bind_button = QPushButton("Bind")
        self.bind_button.clicked.connect(self.bind)
        self.default_button = QPushButton("Reset to default")
        self.default_button.clicked.connect(self.reset_button)
        self.bind_button.setEnabled(False)
        self.default_button.setEnabled(False)
        return well(self.editing, self.picker, row(None, self.default_button, self.bind_button))

    def build_right(self):
        panels = QVBoxLayout()
        panels.setSpacing(10)

        self.stage_rows = []
        self.stage_group = QButtonGroup(self)
        stages = QGridLayout()
        stages.setHorizontalSpacing(8)
        stages.setVerticalSpacing(4)
        for i in range(5):
            use = QRadioButton(f"Stage {i + 1}")
            spin = QSpinBox()
            spin.setRange(100, len(aerox.DPI_LEVELS) * 100)
            spin.setSingleStep(100)
            spin.setKeyboardTracking(False)
            spin.setSuffix(" DPI")
            spin.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
            spin.valueChanged.connect(self.dpi_changed)
            self.stage_group.addButton(use, i)
            stages.addWidget(use, i, 0)
            stages.addWidget(spin, i, 1)
            self.stage_rows.append((use, spin))
        self.stage_group.idClicked.connect(self.stage_picked)
        self.stage_count = QComboBox()
        for n in range(1, 6):
            self.stage_count.addItem(f"{n} stage{'s' if n > 1 else ''}", n)
        self.stage_count.currentIndexChanged.connect(self.count_changed)
        self.polling = QComboBox()
        for hz in sorted(aerox.POLLING_HZ.values(), reverse=True):
            self.polling.addItem(f"{hz} Hz", hz)
        self.polling.currentIndexChanged.connect(self.polling_changed)
        panels.addWidget(well(stages, row(muted("Cycle through"), self.stage_count, None), row(muted("Polling rate"), self.polling, None), title="Sensitivity"))

        self.sleep = QComboBox()
        for ms in TIMERS:
            self.sleep.addItem(timer_label(ms), ms)
        self.sleep.currentIndexChanged.connect(self.sleep_changed)
        self.low_power = QCheckBox("Low power sensor mode")
        self.low_power.toggled.connect(self.low_power_changed)
        panels.addWidget(well(row(muted("Sleep after"), self.sleep, None), self.low_power, title="Power"))

        self.brightness = QSlider(Qt.Horizontal)
        self.brightness.setRange(0, 15)
        self.idle_brightness = QSlider(Qt.Horizontal)
        self.idle_brightness.setRange(0, 15)
        self.idle_timeout = QComboBox()
        for ms in TIMERS:
            self.idle_timeout.addItem(timer_label(ms), ms)
        self.smart = QCheckBox("Smart lighting")
        for slider in (self.brightness, self.idle_brightness):
            slider.valueChanged.connect(self.lighting_changed)
        self.idle_timeout.currentIndexChanged.connect(self.lighting_changed)
        self.smart.toggled.connect(self.lighting_changed)
        self.reactive = QCheckBox("Flash on click")
        self.reactive.toggled.connect(self.reactive_changed)
        self.reactive_random = QCheckBox("Random colour")
        self.reactive_random.toggled.connect(self.reactive_changed)
        self.reactive_color = Swatch()
        self.reactive_color.picked.connect(self.reactive_changed)
        self.zones = []
        zone_row = [muted("Zone colours")]
        for zone in range(3):
            swatch = Swatch("000000")
            swatch.setToolTip(f"Zone {zone + 1}")
            swatch.picked.connect(lambda color, z=zone: self.zone_picked(z, color))
            self.zones.append(swatch)
            zone_row.append(swatch)
        self.clear_zones = QPushButton("Clear")
        self.clear_zones.clicked.connect(self.clear_zone_colors)
        zone_row += [self.clear_zones, None]
        grid = QGridLayout()
        grid.setHorizontalSpacing(8)
        grid.setVerticalSpacing(6)
        grid.addWidget(muted("Brightness"), 0, 0)
        grid.addWidget(self.brightness, 0, 1)
        grid.addWidget(muted("Dim to"), 1, 0)
        grid.addWidget(self.idle_brightness, 1, 1)
        grid.addWidget(muted("Dim after"), 2, 0)
        grid.addLayout(row(self.idle_timeout, None), 2, 1)
        panels.addWidget(well(grid, self.smart, row(self.reactive, self.reactive_color, self.reactive_random, None), row(*zone_row),
                              muted("Zone colours are temporary and can't be saved."), title="Lighting"))
        panels.addStretch(1)
        return panels

    # Loading

    def refresh(self):
        self.runner.submit(self.device.read, self.loaded, self.lost)

    def poll(self):
        """Watches for the mouse waking or sleeping. Only asks for what's on the battery meter."""
        if self.connected:
            self.runner.submit(self.device.battery, self.set_battery, self.lost)
        else:
            self.refresh()

    def showEvent(self, event):
        super().showEvent(event)
        self.poller.start()

    def hideEvent(self, event):
        super().hideEvent(event)
        self.poller.stop()

    def loaded(self, data):
        self.loading = True
        was_connected = self.connected
        self.connected = True
        self.retry.hide()
        self.content.setEnabled(True)
        self.connection.setText("Aerox 9 Wireless")
        self.firmware.setText(("through the dongle" if data["connection"] == "dongle" else "plugged in") + f", firmware {data['firmware']}")
        self.set_battery(data["battery"])
        self.show_polling(data["polling"])
        self.show_dpi(data["dpi"])
        self.select_data(self.sleep, data["sleep_ms"], timer_label)
        self.low_power.setChecked(data["low_power"])
        light = data["lighting"]
        self.brightness.setValue(light["brightness"])
        self.idle_brightness.setValue(light["idle_brightness"])
        self.select_data(self.idle_timeout, light["idle_timeout_ms"], timer_label)
        self.smart.setChecked(light["smart"])
        reactive = data["reactive"]
        self.reactive.setChecked(reactive["enabled"])
        self.reactive_random.setChecked(reactive["random"])
        self.reactive_color.set_color(reactive["color"])
        self.show_buttons(data["buttons"])
        if self.backup is None or not self.dirty:
            self.backup = data["backup"]
        self.loading = False
        if not was_connected or not self.status.text():
            self.say_state()
        self.update_save_state()

    @staticmethod
    def select_data(combo, value, labeller=str):
        index = combo.findData(value)
        if index < 0:  # a value GG's menu doesn't offer: show it rather than hide it
            combo.addItem(labeller(value), value)
            index = combo.count() - 1
        combo.setCurrentIndex(index)

    def show_polling(self, polling):
        if polling["hz"]:
            self.select_data(self.polling, polling["hz"])

    def show_dpi(self, dpi):
        self.stage_count.setCurrentIndex(dpi["count"] - 1)
        for i, (use, spin) in enumerate(self.stage_rows):
            used = i < dpi["count"]
            use.setEnabled(used)
            spin.setEnabled(used)
            if used and dpi["dpi"][i]:
                spin.setValue(dpi["dpi"][i])
            use.setChecked(i == dpi["current"])

    def show_buttons(self, buttons):
        self.buttons_state = {b["name"]: b for b in buttons}
        for b in buttons:
            self.caps[b["name"]].set_detail(pretty_action(b["action"]), marked=not b["default"])
        if self.selected:
            self.select_button(self.selected)

    def set_battery(self, battery):
        self.meter.set_battery(battery)
        state = ", charging" if battery["charging"] else ""
        self.battery_label.setText(f"{battery['percent']}%{state}")
        self.connection.setText("Aerox 9 Wireless")

    def lost(self, error):
        self.connected = False
        self.content.setEnabled(False)
        self.connection.setText("Mouse not reachable")
        self.firmware.setText("")
        self.battery_label.setText("")
        self.meter.clear()
        self.retry.show()
        self.status.say(str(error), "bad")
        self.update_save_state()

    def say_state(self):
        if self.dirty:
            self.status.say("Changes are on the mouse until it powers off. Save to keep them.", "warn")
        else:
            self.status.say("Changes apply right away. Save to keep them after the mouse powers off.")

    def update_save_state(self):
        self.save_button.setEnabled(self.connected and self.dirty)
        self.undo_button.setEnabled(self.connected and self.dirty and self.backup is not None)

    # Applying

    def apply(self, method, *args, then=None, marks_dirty=True, **kwargs):
        def done(value):
            if marks_dirty:
                self.dirty = True
            self.say_state()
            self.update_save_state()
            if then:
                then(value)

        self.runner.submit(lambda: self.device.call(method, *args, **kwargs), done, self.failed)

    def failed(self, error):
        if isinstance(error, aerox.MouseUnreachable):
            self.lost(error)
        else:
            self.status.say(str(error), "bad")

    def current_dpi(self):
        count = self.stage_count.currentData()
        return [self.stage_rows[i][1].value() // 100 * 100 for i in range(count)]

    def dpi_changed(self, *_):
        if not self.loading:
            self.dpi_timer.start()

    def count_changed(self, *_):
        if self.loading:
            return
        count = self.stage_count.currentData()
        for i, (use, spin) in enumerate(self.stage_rows):
            use.setEnabled(i < count)
            spin.setEnabled(i < count)
        if self.stage_group.checkedId() >= count:
            self.stage_rows[0][0].setChecked(True)
        self.dpi_timer.start()

    def stage_picked(self, stage):
        if not self.loading:
            self.apply("set_stage", stage)

    def apply_dpi(self):
        self.apply("set_dpi", self.current_dpi(), max(0, self.stage_group.checkedId()))

    def polling_changed(self, *_):
        if not self.loading:
            self.apply("set_polling", self.polling.currentData())

    def sleep_changed(self, *_):
        if not self.loading:
            self.apply("set_sleep_ms", self.sleep.currentData())

    def low_power_changed(self, checked):
        if not self.loading:
            self.apply("set_low_power", checked)

    def lighting_changed(self, *_):
        if not self.loading:
            self.light_timer.start()

    def apply_lighting(self):
        self.apply("set_lighting", brightness=self.brightness.value(), idle_brightness=self.idle_brightness.value(),
                   smart=self.smart.isChecked(), idle_timeout_ms=self.idle_timeout.currentData())

    def reactive_changed(self, *_):
        if not self.loading:
            self.apply("set_reactive", enabled=self.reactive.isChecked(), random=self.reactive_random.isChecked(),
                       color=self.reactive_color.color)

    def zone_picked(self, zone, color):
        self.apply("set_zone_colors", {zone: color}, marks_dirty=False)

    def clear_zone_colors(self):
        self.apply("clear_zone_colors", marks_dirty=False)

    def select_button(self, name):
        self.selected = name
        self.caps[name].setChecked(True)
        label = BUTTON_LABELS.get(name, name.capitalize())
        self.editing.setText(label)
        self.bind_button.setText(f"Bind to {label}")
        self.bind_button.setEnabled(True)
        self.default_button.setEnabled(True)
        action = self.buttons_state.get(name, {}).get("action", "none")
        self.show_action(action)

    def show_action(self, action):
        self.picker.set_action(action)

    def action_text(self):
        return self.picker.action_text()

    def bind(self):
        if not self.selected:
            return
        try:
            record = aerox.parse_action(self.action_text())
        except aerox.AeroxError as e:
            self.status.say(str(e), "bad")
            return
        self.apply("bind", self.selected, record, then=lambda _: self.reload_buttons())

    def reset_button(self):
        if self.selected:
            record = aerox.DEFAULTS[aerox.record_index(self.selected)]
            self.apply("bind", self.selected, record, then=lambda _: self.reload_buttons())

    def reload_buttons(self):
        self.runner.submit(lambda: self.device.call("buttons"), self.show_buttons, self.failed)

    def save(self):
        if not self.confirm(self, "Save to mouse", "Write your changes to the mouse's flash, so they stay after it powers off?", "Save to mouse"):
            return

        def done(_):
            self.dirty = False
            self.status.say("Saved to the mouse.")
            self.refresh()

        self.runner.submit(lambda: self.device.call("save"), done, self.failed)

    def undo(self):
        if self.backup is None:
            return

        def done(_):
            self.dirty = False
            self.refresh()
            self.status.say("Changes undone.")

        self.runner.submit(lambda: self.device.undo(self.backup), done, self.failed)


class KeyboardPage(QWidget):
    UNIT = 46
    SCREEN_POLL_MS = 400

    def __init__(self, device, runner, confirm=ask, parent=None, oled=None, oled_runner=None):
        super().__init__(parent)
        self.setObjectName("page")
        self.device, self.runner, self.confirm = device, runner, confirm
        self.oled, self.oled_runner = oled, oled_runner or runner
        self.screen_busy = False
        self.screen_fb = None
        self.screen_timer = QTimer(self, interval=self.SCREEN_POLL_MS)
        self.screen_timer.timeout.connect(self.poll_screen)
        self.loading = False
        self.keys = {}
        self.caps = {}
        self.settings = {}
        self.feature_boxes = {}
        self.pressed = set()
        self.stream = None
        self.shortcuts = {}
        self.mode = "actuation"
        self.bindings = None  # as apex-analog bindings --json gives them
        self.controller = {}  # key -> controller output, as kept on this computer
        self.controller_config = dict(CONTROLLER_DEFAULTS, keys={})
        self.controller_on = False
        self.bindings_original = None  # as they were when the page first loaded, for undo
        self.bind_control = None  # a control that isn't on the plate, like the wheel
        self.build()

    def showEvent(self, event):
        super().showEvent(event)
        command = getattr(self.device, "stream_command", None)
        if command:
            if self.stream is None:
                self.stream = DepthStream(command(), self)
                self.stream.changed.connect(self.show_depths)
            self.stream.start()
        self.screen_timer.start()
        self.poll_screen()

    def hideEvent(self, event):
        super().hideEvent(event)
        self.stop_stream()
        self.screen_timer.stop()

    def stop_stream(self):
        if self.stream:
            self.stream.stop()

    def show_depths(self, depths):
        """Fill each key to how far it's pressed. Keys that came up since last time go back to empty."""
        for name in self.pressed | set(depths):
            cap = self.caps.get(name)
            if cap and not cap.inert:
                cap.set_live(depths.get(name, 0.0))
        self.pressed = set(depths)

    def build(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(14, 12, 14, 12)
        root.setSpacing(10)
        self.connection = QLabel("Looking for the keyboard")
        self.connection.setObjectName("title")
        self.firmware = muted("")
        self.retry = QPushButton("Try again")
        self.retry.clicked.connect(self.refresh)
        self.retry.hide()
        root.addWidget(header(self.connection, self.firmware, None, self.retry))

        self.content = QWidget()
        body = QVBoxLayout(self.content)
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(10)
        body.addWidget(self.build_plate())
        controls = QHBoxLayout()
        controls.setSpacing(10)
        controls.addWidget(self.build_left(), 5)
        controls.addWidget(self.build_screen(), 4)
        controls.addWidget(self.build_lighting(), 4)
        controls.addWidget(self.build_setups(), 4)
        body.addLayout(controls, 1)
        root.addWidget(self.content, 1)

        self.status = StatusLine()
        self.save_button = QPushButton("Save to keyboard")
        self.save_button.setObjectName("primary")
        self.save_button.clicked.connect(self.save)
        root.addLayout(row(self.status, self.save_button))

    def build_plate(self):
        self.plate = KeyPlate(self.UNIT)
        self.caps = self.plate.caps
        self.plate.picked.connect(self.selection_changed)
        frame = well()
        frame.layout().addWidget(self.plate, 0, Qt.AlignHCenter)
        return frame

    def build_screen(self):
        self.screen_zoom = OledView(2)
        self.screen_zoom.setFixedSize(SCREEN_WIDTH * 2 + 8, SCREEN_HEIGHT * 2 + 8)
        self.screen_state = muted("")
        self.screen_state.setWordWrap(True)
        self.screen_mode = QComboBox()
        for label, mode in SCREEN_MODES:
            self.screen_mode.addItem(label, mode)
        self.screen_mode.activated.connect(self.mode_picked)
        self.screen_power = QPushButton("Turn screen off")
        self.screen_power.clicked.connect(self.toggle_screen)
        self.screen_text = QLineEdit()
        self.screen_text.setPlaceholderText("Show a message")
        self.screen_text.returnPressed.connect(self.send_message)
        self.screen_send = QPushButton("Show")
        self.screen_send.clicked.connect(self.send_message)
        self.screen_controls = [self.screen_mode, self.screen_power, self.screen_text, self.screen_send]
        panel = well(self.screen_zoom, self.screen_state, self.screen_mode, self.screen_power,
                     row(self.screen_text, self.screen_send), muted("Wheel: volume, press to mute."), muted("Button: play or pause."),
                     title="Screen")
        panel.layout().addStretch(1)
        return panel

    def build_left(self):
        """Actuation or bindings: the same keys, picked on the plate, with a different panel under them."""
        self.mode_buttons = {}
        group = QButtonGroup(self)
        for mode, label in (("actuation", "Actuation"), ("bindings", "Bindings")):
            button = QPushButton(label)
            button.setObjectName("segment")
            button.setCheckable(True)
            button.clicked.connect(lambda checked=False, m=mode: self.set_mode(m))
            bold = button.font()
            bold.setBold(True)
            button.setMinimumWidth(QFontMetrics(bold).horizontalAdvance(label) + 28)  # room for the bold checked label
            group.addButton(button)
            self.mode_buttons[mode] = button
        self.mode_buttons["actuation"].setChecked(True)
        self.mode_stack = QStackedWidget()
        self.mode_stack.addWidget(self.build_actuation())
        self.mode_stack.addWidget(self.build_bindings())
        self.selection_changed()
        self.controller_button = QPushButton("Controller\u2026")
        self.controller_button.setToolTip("Keys as a game controller, axes and buttons, in a window of its own")
        self.controller_button.clicked.connect(self.open_controller)
        self.controller_window = None
        box = QWidget()
        layout = QVBoxLayout(box)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)
        layout.addLayout(row(self.mode_buttons["actuation"], self.mode_buttons["bindings"], None, self.controller_button))
        layout.addWidget(self.mode_stack, 1)
        return box

    def build_bindings(self):
        self.bind_title = QLabel("Pick a key to rebind")
        self.bind_title.setObjectName("title")
        self.bind_layer = QComboBox()
        for label, layer in BIND_LAYERS:
            self.bind_layer.addItem(label, layer)
        self.bind_layer.currentIndexChanged.connect(self.layer_changed)
        self.bind_other = QComboBox()
        self.bind_other.addItem("Or pick another control", None)
        for label, name in OTHER_CONTROLS:
            self.bind_other.addItem(label, name)
        self.bind_other.activated.connect(self.other_picked)
        self.bind_picker = ActionPicker(BIND_ACTION_KINDS)
        self.bind_apply = QPushButton("Bind")
        self.bind_apply.clicked.connect(self.apply_binding)
        self.bind_reset = QPushButton("Reset to default")
        self.bind_reset.clicked.connect(self.reset_binding)
        self.bind_undo = QPushButton("Undo binding changes")
        self.bind_undo.clicked.connect(self.undo_bindings)
        self.bind_note = muted("")
        self.bind_note.setWordWrap(True)
        self.deep_depth = QComboBox()
        self.deep_depth.addItem("the bottom (off)", 0.0)
        for mm in DEPTHS:
            self.deep_depth.addItem(f"{mm:.1f} mm", mm)
        self.deep_set = QPushButton("Set")
        self.deep_set.clicked.connect(self.apply_deep)
        self.deep_row = QWidget()
        deep_layout = row(muted("Deeper press starts at"), self.deep_depth, self.deep_set, None)
        deep_layout.setContentsMargins(0, 0, 0, 0)
        self.deep_row.setLayout(deep_layout)
        hint = muted("Bindings live on the keyboard itself, so they work with no software running. They last until you unplug it, "
                     "and Save to keyboard keeps them. For text, commands or macros, use Shortcuts.")
        hint.setWordWrap(True)
        self.changed_title = QLabel("Your bindings")
        self.changed_title.setObjectName("title")
        self.ss_lights = QPushButton("Light up SteelSeries-layer keys")
        self.ss_lights.setToolTip("While the SteelSeries key is held, every key bound on that layer glows. New bindings do this by themselves.")
        self.ss_lights.clicked.connect(lambda: self.apply(self.device.sync_ss_lights, "Every key bound on the SteelSeries layer lights up while it's held."))
        self.changed_list = QVBoxLayout()
        self.changed_list.setSpacing(4)
        panel = well(self.bind_title, self.bind_layer, self.bind_other, self.deep_row, self.bind_picker,
                     row(self.bind_reset, self.bind_apply, None), self.bind_note, hint, row(self.bind_undo, self.ss_lights, None),
                     self.changed_title)
        panel.layout().addLayout(self.changed_list)
        self.deep_row.hide()
        panel.layout().addStretch(1)
        return panel

    def build_actuation(self):
        self.selection_label = QLabel("Pick keys to change")
        self.selection_label.setObjectName("title")
        pick = []
        for label, names in (("W A S D", ["W", "A", "S", "D"]), ("All", None), ("None", [])):
            button = QPushButton(label)
            button.clicked.connect(lambda checked=False, n=names: self.pick(n))
            pick.append(button)
        self.depth = QSlider(Qt.Horizontal)
        self.depth.setRange(0, len(DEPTHS) - 1)
        self.depth.setValue(DEPTHS.index(1.0))
        self.depth.valueChanged.connect(self.depth_shown)
        self.depth_label = QLabel()
        self.depth_label.setFixedWidth(52)
        self.depth_shown()
        self.set_depth = QPushButton("Set actuation")
        self.set_depth.clicked.connect(self.apply_actuation)
        self.rapid_on = QPushButton("Turn rapid tap on")
        self.rapid_on.clicked.connect(lambda: self.apply_rapid(True))
        self.rapid_off = QPushButton("Turn rapid tap off")
        self.rapid_off.clicked.connect(lambda: self.apply_rapid(False))
        check = QPushButton("Check switches")
        check.clicked.connect(lambda: self.show_report("health"))
        typing = QPushButton("How I type")
        typing.clicked.connect(lambda: self.show_report("profile"))
        self.report = muted("")
        self.report.setWordWrap(True)
        pick_hint = muted("Drag across keys to pick several, or drag from an empty spot to box them.")
        pick_hint.setWordWrap(True)
        fill_hint = muted("Shading shows where a key fires. Keys fill as you press them.")
        fill_hint.setWordWrap(True)
        self.report.setAlignment(Qt.AlignTop | Qt.AlignLeft)
        panel = well(self.selection_label, row(*pick, None), pick_hint, row(muted("Fires at"), self.depth, self.depth_label),
                     row(self.set_depth, None), row(self.rapid_on, self.rapid_off, None),
                     fill_hint,
                     row(check, typing, None), self.report)
        panel.layout().addStretch(1)
        return panel

    def build_lighting(self):
        self.effect = QComboBox()
        self.effect.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
        self.effect.setMinimumContentsLength(14)
        for label, args, _ in EFFECTS:
            self.effect.addItem(label, args)
        self.effect.currentIndexChanged.connect(self.effect_picked)
        self.effect_color = Swatch("ffffff")
        self.effect_color.setToolTip("Colour of a pressed key")
        apply_effect = QPushButton("Use this lighting")
        apply_effect.clicked.connect(self.apply_effect)
        self.feature_list = QVBoxLayout()
        for name, label, hint in FEATURES:
            box = QCheckBox(label)
            box.setStyleSheet("QCheckBox { spacing: 6px; }")
            box.setToolTip(hint)
            box.clicked.connect(lambda checked=False, n=name: self.toggle_feature(n, checked))
            self.feature_boxes[name] = box
            self.feature_list.addWidget(box)
        self.effect_picked()
        panel = well(self.effect, row(muted("Pressed key"), self.effect_color, None), apply_effect, title="Lighting")
        features_title = QLabel("Features")
        features_title.setObjectName("title")
        panel.layout().addWidget(features_title)
        panel.layout().addLayout(self.feature_list)
        self.jobs_label = muted("")
        self.jobs_label.setWordWrap(True)
        self.turn_off = QVBoxLayout()
        running = QLabel("Running now")
        running.setObjectName("title")
        panel.layout().addWidget(running)
        panel.layout().addWidget(self.jobs_label)
        panel.layout().addLayout(self.turn_off)
        panel.layout().addStretch(1)
        return panel

    def build_setups(self):
        self.preset = QComboBox()
        self.preset.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.preset.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
        self.preset.setMinimumContentsLength(8)
        use = QPushButton("Use")
        use.clicked.connect(self.use_preset)
        save_as = QPushButton("Save as new")
        save_as.clicked.connect(self.save_preset)
        delete = QPushButton("Delete")
        delete.clicked.connect(self.delete_preset)
        panel = well(row(self.preset, use), row(save_as, delete, None), title="Saved setups")
        title = QLabel("Shortcuts")
        title.setObjectName("title")
        hint = muted("Hold the SteelSeries key and press another key, or pick the SteelSeries key itself for a quick tap. Red keys are the keyboard's own.")
        hint.setWordWrap(True)
        self.shortcut_key = QLabel("Pick one key")
        self.shortcut_kind = QComboBox()
        self.shortcut_kind.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
        self.shortcut_kind.setMinimumContentsLength(10)
        for label, kind, _ in SHORTCUT_KINDS:
            self.shortcut_kind.addItem(label, kind)
        self.shortcut_kind.currentIndexChanged.connect(self.shortcut_kind_picked)
        self.shortcut_value = QLineEdit()
        self.shortcut_save = QPushButton("Save shortcut")
        self.shortcut_save.clicked.connect(self.save_shortcut)
        self.shortcut_remove = QPushButton("Remove")
        self.shortcut_remove.clicked.connect(self.remove_shortcut)
        self.shortcut_list = muted("")
        self.shortcut_list.setWordWrap(True)
        for widget in (title, hint, self.shortcut_key, self.shortcut_kind, self.shortcut_value):
            panel.layout().addWidget(widget)
        panel.layout().addLayout(row(self.shortcut_save, self.shortcut_remove, None))
        panel.layout().addWidget(self.shortcut_list)
        panel.layout().addStretch(1)
        self.shortcuts = {}
        self.shortcut_kind_picked()
        return panel

    # Loading

    def refresh(self):
        self.runner.submit(self.device.read, self.loaded, self.lost)

    def loaded(self, data):
        self.loading = True
        self.retry.hide()
        self.content.setEnabled(True)
        self.save_button.setEnabled(True)
        info = data["info"]
        self.connection.setText(info.get("model", "Apex Pro Gen 3"))
        self.firmware.setText(f"firmware {info['firmware']}, {len(info['keys'])} keys with sensors")
        self.settings = info["keys"]
        self.shortcuts = data.get("shortcuts", {})
        self.controller_config = data.get("controller") or dict(CONTROLLER_DEFAULTS, keys={})
        self.controller = self.controller_config["keys"]
        if self.controller_window:
            self.controller_window.loaded(self.controller_config, self.controller_on, self.settings)
        self.controller_on = any(j["on"] and j["name"] == "input" and j["command"] == "map" for j in data["jobs"])
        self.bindings = data.get("bindings")
        if self.bindings and self.bindings_original is None:
            self.bindings_original = self.bindings
        self.mode_buttons["bindings"].setEnabled(self.bindings is not None)
        self.mode_buttons["bindings"].setToolTip("" if self.bindings is not None else "This needs a newer apex-analog.")
        if self.bindings is None and self.mode == "bindings":
            self.set_mode("actuation")
        self.show_caps()
        current = self.preset.currentText()
        self.preset.clear()
        self.preset.addItems(data["presets"])
        if current in data["presets"]:
            self.preset.setCurrentText(current)
        self.show_jobs(data["jobs"])
        self.shortcut_list.setText("\n".join(f"{shortcut_label(k)}: {v}" for k, v in sorted(self.shortcuts.items())))
        self.loading = False
        self.selection_changed()
        if info.get("changes") == "experimental":
            self.status.say(f"Changing settings on the {info.get('model', 'keyboard')} is experimental: nobody has tried it on a real one yet. "
                            "Unplugging undoes anything not saved.", "warn")
        elif not self.status.text():
            self.status.say("Changes apply right away. Save to keep them after a replug.")

    def set_cap_live(self, cap, live):
        cap.inert = not live
        cap.setEnabled(live)
        cap.setFocusPolicy(Qt.StrongFocus if live else Qt.NoFocus)
        if not live:
            cap.setChecked(False)

    def show_caps(self):
        if self.mode == "bindings" and self.bindings:
            self.show_binding_caps()
        else:
            self.show_actuation_caps()

    def show_actuation_caps(self):
        for name, cap in self.caps.items():
            key = self.settings.get(name)
            self.set_cap_live(cap, bool(key))
            if key:
                mm = self.key_depth(key)
                rapid = key["mode"] != 0
                cap.fires_at = key["press"]
                shortcut = self.shortcuts.get(name)
                cap.set_detail(f"{mm:.1f}" + (" R" if rapid else ""), fill=key["press"] / TRAVEL_MM, marked=rapid or bool(shortcut))
                cap.setToolTip(f"Fires at {mm:.1f} mm" + (", rapid tap on" if rapid else "") + (f". With the SteelSeries key: {shortcut}" if shortcut else ""))
            else:
                cap.set_detail("")
                cap.setToolTip("")

    def bind_layer_name(self):
        return self.bind_layer.currentData()

    def binding_entry(self, name, layer=None):
        """This key's binding on the picked layer, or None if it can't have one there."""
        layer = layer or self.bind_layer_name()
        if not self.bindings:
            return None
        if name in ("WHEEL_UP", "WHEEL_DOWN"):
            return self.bindings.get("wheel", {}).get(layer, {}).get(name)
        return self.bindings["layers"].get(layer, {}).get(name)

    def show_binding_caps(self):
        layer = self.bind_layer_name()
        for name, cap in self.caps.items():
            entry = self.binding_entry(name, layer)
            self.set_cap_live(cap, entry is not None)
            if entry:
                cap.set_detail(binding_label(entry, layer), fill=None, marked=not entry["default"])
                action = "one of the keyboard's own functions" if entry["action"].startswith("builtin:") else entry["action"]
                cap.setToolTip(f"{BIND_LAYERS[self.bind_layer.currentIndex()][0]}: {action}" + ("" if entry["default"] else " (changed)"))
            else:
                cap.set_detail("")
                cap.setToolTip("No sensor, so no deeper press" if layer == "deep" else "")

    def set_mode(self, mode):
        if mode == "bindings" and not self.bindings:
            mode = "actuation"
        self.mode = mode
        self.mode_buttons[mode].setChecked(True)
        self.mode_stack.setCurrentIndex(0 if mode == "actuation" else 1)
        for cap in self.caps.values():
            cap.setChecked(False)
        self.bind_control = None
        self.bind_other.setCurrentIndex(0)
        self.show_caps()
        self.selection_changed()

    def lost(self, error):
        self.content.setEnabled(False)
        self.save_button.setEnabled(False)
        self.connection.setText("Keyboard not reachable")
        self.firmware.setText("")
        self.retry.show()
        self.status.say(str(error), "bad")

    @staticmethod
    def key_depth(key):
        """The level this key is set to. `release` is the closest reading to the level's own number."""
        return min(DEPTHS, key=lambda d: abs(d - key["release"]))

    def show_jobs(self, jobs):
        running = [j for j in jobs if j["on"]]
        names = {j["name"] for j in running}
        self.show_effect(next((j["command"] for j in running if j["name"] == "lights"), None))
        for name, box in self.feature_boxes.items():
            box.setChecked(name in names)
        while self.turn_off.count():
            item = self.turn_off.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        self.jobs_label.setText("Nothing else is running." if not running else "")
        self.jobs_label.setVisible(not running)
        for job in running:
            button = QPushButton(f"Turn off {job['name']}")
            button.setToolTip(job["command"])
            button.clicked.connect(lambda checked=False, n=job["name"]: self.stop_job(n))
            self.turn_off.addWidget(button)

    def show_effect(self, command):
        """Show the lighting that's really running, not just the last thing picked here."""
        words = command.split() if command else None
        self.effect.setCurrentIndex(0)
        for i, (_, args, _) in enumerate(EFFECTS):
            if args and words and args == [w for w in words if not re.fullmatch(r"[0-9a-fA-F]{6}", w) and w != "--color"]:
                self.effect.setCurrentIndex(i)
        if words and "--color" in words:
            self.effect_color.set_color(words[words.index("--color") + 1].lower())

    def effect_picked(self, *_):
        default = EFFECTS[self.effect.currentIndex()][2]
        self.effect_color.setEnabled(default is not None)
        if default and not self.loading:
            self.effect_color.set_color(default)

    # The screen

    def poll_screen(self):
        if self.oled is None:
            self.screen_lost(KeyboardError("apex-oled isn't set up here."))
            return
        if not self.screen_busy:
            self.screen_busy = True
            self.oled_runner.submit(self.oled.frame, self.show_screen, self.screen_lost)

    def show_screen(self, state):
        self.screen_busy = False
        for control in self.screen_controls:
            control.setEnabled(True)
        fb = None if state.get("blanked") else state.get("fb")
        if fb != self.screen_fb:
            self.screen_fb = fb
            image = oled_image(base64.b64decode(fb)) if fb else None
            self.plate.screen.set_image(image)
            self.screen_zoom.set_image(image)
        forced = state.get("forced_mode")
        self.screen_mode.setCurrentIndex(max(0, self.screen_mode.findData(forced or "auto")))
        manual = state.get("blanked") and state.get("blank_reason") == "manual"
        self.screen_power.setText("Turn screen on" if manual else "Turn screen off")
        self.screen_state.setText(self.describe_screen(state, manual))

    @staticmethod
    def describe_screen(state, manual):
        if state.get("outdated"):
            return "Restart apex-oledd to see the screen here."
        if state.get("released"):
            return "Another program is drawing on it."
        if manual:
            return "Turned off."
        if state.get("blanked"):
            return "Dark until you're back."
        forced = state.get("forced_mode")
        if forced:
            return f"Pinned to {SCREEN_SOURCES.get(forced, forced)}: nothing else can take the screen."
        source = state.get("source")
        return f"Showing {SCREEN_SOURCES.get(source, source)}." if source else ""

    def screen_lost(self, error):
        self.screen_busy = False
        self.screen_fb = None
        self.plate.screen.set_image(None)
        self.screen_zoom.set_image(None)
        for control in self.screen_controls:
            control.setEnabled(False)
        self.screen_state.setText(str(error))

    def screen_command(self, call):
        self.oled_runner.submit(call, lambda _: self.poll_screen(), self.screen_lost)

    def mode_picked(self, *_):
        mode = self.screen_mode.currentData()
        self.screen_command(lambda: self.oled.set_mode(mode))

    def toggle_screen(self):
        self.screen_command(self.oled.toggle_power)

    def send_message(self):
        text = self.screen_text.text().strip()
        if text:
            self.screen_text.clear()
            self.screen_command(lambda: self.oled.show_message(text))

    # Selecting keys

    def selected_keys(self):
        return [n for n, cap in self.caps.items() if cap.isChecked()]

    def pick(self, names):
        wanted = set(self.settings) if names is None else set(names)
        for name, cap in self.caps.items():
            if not cap.inert:
                cap.setChecked(name in wanted)
        self.selection_changed()

    def selection_changed(self, *_):
        chosen = self.selected_keys()
        if not chosen:
            text = "Pick keys to change"
        elif len(chosen) <= 6:
            text = "Picked " + " ".join(KEY_LABELS.get(n, n) for n in chosen)
        else:
            text = f"{len(chosen)} keys picked"
        self.selection_label.setText(text)
        for button in (self.set_depth, self.rapid_on, self.rapid_off):
            button.setEnabled(bool(chosen))
        if len(chosen) == 1 and chosen[0] in self.settings:
            self.depth.setValue(DEPTHS.index(self.key_depth(self.settings[chosen[0]])))
        self.show_set_label()
        self.show_shortcut_key()
        if chosen and hasattr(self, "bind_other"):
            self.bind_control = None
            self.bind_other.setCurrentIndex(0)
        self.show_binding_target()

    # Bindings

    def bind_targets(self):
        return [self.bind_control] if self.bind_control else self.selected_keys()

    def target_label(self, names):
        if len(names) > 6:
            return f"{len(names)} keys"
        return " ".join(CONTROL_LABELS.get(n) or KEY_LABELS.get(n, n) for n in names)

    def bind_refusal(self, names):
        """Why these can't be bound on this layer, or None."""
        layer = self.bind_layer_name()
        if not names:
            return None
        if layer == "base" and "SS" in names and self.bindings and self.bindings.get("layer_key") == "SS":
            return "The SteelSeries key turns on its layer while held, so bind it while held instead: that fires on each press."
        missing = [n for n in names if self.binding_entry(n) is None]
        if missing:
            return f"{self.target_label(missing)} can't do anything on a deeper press: no sensor." if layer == "deep" else f"{self.target_label(missing)} can't be bound."
        return None

    def show_binding_target(self):
        if not hasattr(self, "bind_apply"):
            return
        names = self.bind_targets()
        refusal = self.bind_refusal(names)
        when = BIND_LAYERS[self.bind_layer.currentIndex()][0]
        self.bind_title.setText(f"{self.target_label(names)}, {when[:1].lower()}{when[1:]}" if names else "Pick a key to rebind")
        ok = bool(names) and refusal is None and bool(self.bindings)
        self.bind_apply.setEnabled(ok)
        self.bind_reset.setEnabled(ok)
        deep = self.bind_layer_name() == "deep"
        self.deep_row.setVisible(deep)
        self.deep_set.setEnabled(ok and deep)
        note = refusal or ""
        if not note and deep and names:
            points = {self.settings.get(n, {}).get("deep") for n in names}
            if points == {None}:
                note = "The deeper press starts at the very bottom until you set where it starts."
            elif len(points) == 1:
                note = f"The deeper press starts at {points.pop():.1f} mm."
        if deep and len(names) == 1:
            point = self.settings.get(names[0], {}).get("deep")
            self.deep_depth.setCurrentIndex(max(0, self.deep_depth.findData(point if point else 0.0)))
        self.bind_note.setText(note)
        if ok:
            entry = self.binding_entry(names[0])
            action = entry["action"]
            if not (action.startswith("builtin:") or action.startswith("raw:")):
                self.bind_picker.set_action(action)
        self.bind_undo.setEnabled(bool(self.bindings_original and self.bindings and binding_changes(self.bindings_original, self.bindings)))
        self.show_changed_bindings()

    def changed_bindings(self, layer):
        """[(key, entry)] on this layer that differ from what the keyboard does out of the box."""
        if not self.bindings:
            return []
        entries = {**self.bindings["layers"].get(layer, {}), **self.bindings.get("wheel", {}).get(layer, {})}
        return [(key, entry) for key, entry in entries.items() if not entry["default"]]

    def show_changed_bindings(self):
        """Each key changed on the picked layer, with a button that puts just that one back."""
        if not hasattr(self, "changed_list"):
            return
        while self.changed_list.count():
            item = self.changed_list.takeAt(0)
            widgets = [item.widget()] if item.widget() else []
            if item.layout():
                while item.layout().count():
                    widgets.append(item.layout().takeAt(0).widget())
            for widget in widgets:
                if widget:
                    widget.hide()  # hidden first, so nothing lingers on screen before Qt deletes it
                    widget.deleteLater()
        layer = self.bind_layer_name()
        changed = self.changed_bindings(layer)
        when = BIND_LAYERS[self.bind_layer.currentIndex()][0]
        self.changed_title.setText(f"Your bindings, {when[:1].lower()}{when[1:]}")
        if not changed:
            self.changed_list.addWidget(muted("None on this layer: every key does what it does out of the box."))
            return
        for key, entry in changed:
            name = CONTROL_LABELS.get(key) or KEY_LABELS.get(key, key)
            action = "one of the keyboard's own functions" if entry["action"].startswith("builtin:") else pretty_action(entry["action"])
            action = action[4:] if action.startswith("Key ") else action
            remove = QPushButton("Remove")
            remove.setToolTip(f"Put {name} back to what it does out of the box on this layer")
            remove.clicked.connect(lambda checked=False, k=key, n=name, l=layer: self.remove_binding(k, n, l))
            self.changed_list.addLayout(row(muted(f"{name}: {action}"), None, remove))

    def remove_binding(self, key, name, layer):
        self.apply(lambda: self.device.bind([key], "default", layer), f"{name} is back to normal.")

    def layer_changed(self, *_):
        if self.mode == "bindings":
            self.show_caps()
        self.show_binding_target()

    def other_picked(self, *_):
        self.bind_control = self.bind_other.currentData()
        if self.bind_control:
            for cap in self.caps.values():
                cap.setChecked(False)
        self.selection_changed()

    def apply_binding(self):
        names, action, layer = self.bind_targets(), self.bind_picker.action_text(), self.bind_layer_name()
        if not names or self.bind_refusal(names):
            return
        if not action:
            self.status.say("Say what it should do first.", "bad")
            return
        self.apply(lambda: self.device.bind(names, action, layer), f"{self.target_label(names)} now does {pretty_action(action)}.")

    def reset_binding(self):
        names, layer = self.bind_targets(), self.bind_layer_name()
        if names and not self.bind_refusal(names):
            self.apply(lambda: self.device.bind(names, "default", layer), f"{self.target_label(names)} is back to normal.")

    def apply_deep(self):
        names, mm = self.bind_targets(), self.deep_depth.currentData()
        if not names or self.bind_refusal(names):
            return
        where = f"{mm:.1f} mm" if mm else "the bottom"
        self.apply(lambda: self.device.set_deep(names, mm), f"The deeper press on {self.target_label(names)} starts at {where}.")

    def undo_bindings(self):
        if not (self.bindings_original and self.bindings):
            return
        items = binding_changes(self.bindings_original, self.bindings)
        if not items:
            self.status.say("Nothing to undo.")
            return
        self.apply(lambda: self.device.restore_bindings(items), "Bindings are back to how they were when this page opened.")

    def shortcut_target(self):
        """The one picked key a shortcut can go on, or None."""
        chosen = self.selected_keys()
        return chosen[0] if len(chosen) == 1 and chosen[0] not in SHORTCUT_TAKEN else None

    def show_shortcut_key(self):
        if not hasattr(self, "shortcut_save"):
            return
        chosen, target = self.selected_keys(), self.shortcut_target()
        if target:
            self.shortcut_key.setText(shortcut_label(target))
        elif len(chosen) == 1:
            self.shortcut_key.setText(f"{KEY_LABELS.get(chosen[0], chosen[0])} is the keyboard's own")
        else:
            self.shortcut_key.setText("Pick one key")
        self.shortcut_save.setEnabled(bool(target))
        self.shortcut_remove.setEnabled(bool(target) and target in self.shortcuts)

    def shortcut_kind_picked(self, *_):
        _, kind, placeholder = SHORTCUT_KINDS[self.shortcut_kind.currentIndex()]
        self.shortcut_value.setPlaceholderText(placeholder)
        self.shortcut_value.setVisible(bool(placeholder))

    def save_shortcut(self):
        key, kind, value = self.shortcut_target(), self.shortcut_kind.currentData(), self.shortcut_value.text().strip()
        if not key:
            return
        if kind in ("keys", "text", "command") and not value:
            self.status.say("Say what the shortcut should do first.", "bad")
            return
        self.shortcut_value.clear()
        self.apply(lambda: self.device.add_shortcut(key, kind, value), f"Saved the shortcut on {KEY_LABELS.get(key, key)}.")

    def remove_shortcut(self):
        key = self.shortcut_target()
        if key:
            self.apply(lambda: self.device.remove_shortcut(key), f"Removed the shortcut on {KEY_LABELS.get(key, key)}.")

    def depth_shown(self, *_):
        self.depth_label.setText(f"{DEPTHS[self.depth.value()]:.1f} mm")
        if hasattr(self, "set_depth"):
            self.show_set_label()

    def show_set_label(self):
        count = len(self.selected_keys())
        self.set_depth.setText(f"Set {count} key{'s' if count != 1 else ''} to {DEPTHS[self.depth.value()]:.1f} mm" if count else "Set actuation")

    def show_report(self, name):
        self.report.setText("Reading the keyboard.")
        self.runner.submit(lambda: self.device.report(name), self.report.setText, lambda e: self.report.setText(str(e)))

    # Applying

    def apply(self, call, message):
        def done(_):
            self.status.say(message)
            self.refresh()

        self.runner.submit(call, done, lambda e: self.status.say(str(e), "bad"))

    def apply_actuation(self):
        names, mm = self.selected_keys(), DEPTHS[self.depth.value()]
        if names:
            self.apply(lambda: self.device.set_actuation(names, mm), f"Set {len(names)} key{'s' if len(names) != 1 else ''} to {mm:.1f} mm.")

    def apply_rapid(self, on):
        names = self.selected_keys()
        if names:
            state = "on" if on else "off"
            self.apply(lambda: self.device.set_rapid(names, on), f"Turned rapid tap {state} for {len(names)} key{'s' if len(names) != 1 else ''}.")

    def apply_effect(self):
        args = self.effect.currentData()
        default = EFFECTS[self.effect.currentIndex()][2]
        color = self.effect_color.color if default and self.effect_color.color != default else None
        self.apply(lambda: self.device.lights(args, color), f"Lighting set: {self.effect.currentText().lower()}.")

    def open_controller(self):
        if self.controller_window is None:
            self.controller_window = ControllerWindow(self.device, self.runner, self.refresh, self)
        self.controller_window.loaded(self.controller_config, self.controller_on, self.settings)
        self.controller_window.show()
        self.controller_window.raise_()
        self.controller_window.activateWindow()

    def toggle_feature(self, name, on):
        call = (lambda: self.device.start(name)) if on else (lambda: self.device.stop(name))
        label = dict((n, l) for n, l, _ in FEATURES)[name]
        self.apply(call, f"{label}: {'on' if on else 'off'}.")

    def stop_job(self, name):
        self.apply(lambda: self.device.stop(name), f"Turned off {name}.")

    def use_preset(self):
        name = self.preset.currentText()
        if name:
            self.apply(lambda: self.device.use_preset(name), f"Using {name}.")

    def save_preset(self):
        name, ok = self.ask_name()
        if ok and name.strip():
            name = name.strip()
            self.apply(lambda: self.device.save_preset(name), f"Saved {name}.")

    def ask_name(self):
        return QInputDialog.getText(self, "Save as new", "Name this setup:")

    def delete_preset(self):
        name = self.preset.currentText()
        if name and self.confirm(self, "Delete setup", f"Delete {name} from this computer?", "Delete"):
            self.apply(lambda: self.device.delete_preset(name), f"Deleted {name}.")

    def save(self):
        if self.confirm(self, "Save to keyboard", "Write the current settings to the keyboard's flash, replacing its active saved config?", "Save to keyboard"):
            self.apply(self.device.save, "Saved to the keyboard.")


SIDETONE_LEVELS = [("Off", "off"), ("Low", "low"), ("Medium", "medium"), ("High", "high")]
EQ_PRESET_LABELS = [("Flat", 0), ("Bass boost", 1), ("Reference", 2), ("Smiley", 3), ("Custom", 4)]
SLEEP_MINUTES = [0, 5, 10, 15, 30, 60, 90]


class ControllerWindow(QWidget):
    """Keys as a game controller, in a window of its own. Pick keys on the keyboard, click what they should be, and it runs.
    The setup is kept on this computer. The rules box holds the same setup as apex-analog map rules, for typing it directly."""

    UNIT = 46
    ROLE_GROUPS = [
        ("Left stick", [("\u2191", "ly-"), ("\u2193", "ly+"), ("\u2190", "lx-"), ("\u2192", "lx+")]),
        ("Right stick", [("\u2191", "ry-"), ("\u2193", "ry+"), ("\u2190", "rx-"), ("\u2192", "rx+")]),
        ("Triggers", [("LT", "lt"), ("RT", "rt")]),
        ("Bumpers", [("LB", "lb"), ("RB", "rb")]),
        ("Buttons", [("A", "a"), ("B", "b"), ("X", "x"), ("Y", "y")]),
        ("Stick clicks", [("L3", "ls"), ("R3", "rs")]),
        ("D-pad", [("\u2191", "dpad-up"), ("\u2193", "dpad-down"), ("\u2190", "dpad-left"), ("\u2192", "dpad-right")]),
        ("Menu", [("Back", "back"), ("Start", "start"), ("Guide", "guide")]),
    ]

    def __init__(self, device, runner, changed, parent=None):
        super().__init__(parent, Qt.Window)
        self.setWindowTitle("Controller")
        self.setObjectName("page")
        self.device, self.runner, self.changed = device, runner, changed
        self.config = dict(CONTROLLER_DEFAULTS, keys={})
        self.running = False
        self.sensor_keys = set()
        root = QVBoxLayout(self)
        root.setContentsMargins(14, 12, 14, 12)
        root.setSpacing(10)

        title = QLabel("Controller")
        title.setObjectName("title")
        self.state = muted("")
        self.run_button = QPushButton("Start")
        self.run_button.setObjectName("primary")
        self.run_button.clicked.connect(self.toggle_running)
        root.addLayout(row(title, self.state, None, self.run_button))

        self.plate = KeyPlate(self.UNIT)
        self.plate.picked.connect(self.selection_changed)
        self.plate.screen.hide()  # the screen and the wheel aren't part of the controller
        self.caps = self.plate.caps
        root.addWidget(self.plate, 0, Qt.AlignHCenter)

        # what the picked keys do
        self.picked_label = QLabel("Pick keys on the keyboard")
        self.picked_label.setObjectName("title")
        grid = QGridLayout()
        grid.setHorizontalSpacing(6)
        grid.setVerticalSpacing(6)
        self.role_buttons = []
        for r, (label, items) in enumerate(self.ROLE_GROUPS):
            grid.addWidget(muted(label), r, 0)
            for c, (text, out) in enumerate(items):
                button = QPushButton(text)
                button.setToolTip(f"The picked keys act as {controller_label(out)}" + (", by how far they're pressed"
                                                                                       if out[:2] in ("lx", "ly", "rx", "ry", "lt", "rt") else ""))
                button.setMinimumWidth(max(52, button.fontMetrics().horizontalAdvance(text) + 24))
                button.clicked.connect(lambda checked=False, o=out: self.assign(o))
                grid.addWidget(button, r, 1 + c)
                self.role_buttons.append(button)
        self.own_axis = QPushButton("An axis each")
        self.own_axis.setToolTip("Each picked key gets an axis of its own: 0 at rest, full at the bottom. Eight axes to a virtual device.")
        self.own_axis.clicked.connect(self.assign_own_axes)
        self.shared_axis = QPushButton("Two keys, one axis")
        self.shared_axis.setToolTip("Pick two keys. The axis rests in the middle: the upper key on the keyboard moves it up, "
                                    "the other one down, each by how far it's pressed.")
        self.shared_axis.clicked.connect(self.assign_shared_axis)
        self.type_again = QPushButton("Type normally")
        self.type_again.setToolTip("The picked keys leave the controller and type again")
        self.type_again.clicked.connect(self.unassign)
        roles = well(self.picked_label, grid, muted("Axes"), row(self.own_axis, self.shared_axis, None), row(self.type_again, None))
        roles.layout().addStretch(1)

        # what's set now
        quick = QGridLayout()
        quick.setSpacing(6)
        for i, name in enumerate([*CONTROLLER_LAYOUTS, "Clear all"]):
            button = QPushButton(name)
            button.clicked.connect(lambda checked=False, n=name: self.quick_setup(n))
            quick.addWidget(button, i // 2, i % 2)
        self.table = QTableWidget(0, 3)
        self.table.setHorizontalHeaderLabels(["Key", "Does", ""])
        self.table.verticalHeader().hide()
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        self.table.setMinimumHeight(200)
        self.table.itemSelectionChanged.connect(self.table_picked)
        current = well(muted("Quick setups"), quick, muted("Set now"), self.table, title="Setup")

        # how it responds, and the rules
        self.toggle = QComboBox()
        self.toggle.addItem("No toggle", None)
        for key in TOGGLE_KEYS:
            self.toggle.addItem(f"SteelSeries + {key}", key)
        self.toggle.setToolTip("Hold the SteelSeries key and press this to turn the controller off, so the keys type, and on again")
        self.on_color, self.off_color = Swatch("00ff00"), Swatch("ff0000")
        self.on_color.setToolTip("While SteelSeries is held, the toggle key glows this colour when the controller is on")
        self.off_color.setToolTip("...and this colour when it's off")
        self.deadzone, self.full, self.curve = QDoubleSpinBox(), QDoubleSpinBox(), QDoubleSpinBox()
        for spin, low, high, suffix in ((self.deadzone, 0.0, 3.9, " mm"), (self.full, 0.2, 4.0, " mm"), (self.curve, 0.2, 4.0, "")):
            spin.setRange(low, high)
            spin.setSingleStep(0.1)
            spin.setDecimals(1)
            spin.setSuffix(suffix)
        self.deadzone.setToolTip("Sticks, triggers and axes stay at rest until a key is this deep")
        self.full.setToolTip("...and are all the way at this depth")
        self.curve.setToolTip("1 is straight. Above 1 gives finer control near the top of the press")
        self.type_too = QCheckBox("Controller keys keep typing too")
        self.type_too.setToolTip("Normally keys on the controller stop typing while it runs")
        apply_settings = QPushButton("Use these settings")
        apply_settings.clicked.connect(lambda: self.submit(dict(self.config["keys"]), "Settings saved."))
        self.rules = QPlainTextEdit()
        self.rules.setPlaceholderText("W=ly-\nS=ly+\nLSHIFT=axis:1+\nLCTRL=axis:1-")
        self.rules.setToolTip("One KEY=OUTPUT per line, as apex-analog map takes them. Outputs: lx- lx+ ly- ly+ rx- rx+ ry- ry+, "
                              "lt rt, a b x y lb rb ls rs back start guide, dpad-up dpad-down dpad-left dpad-right, axis:1 to axis:64, "
                              "and axis:N+ with axis:N- for two keys sharing one. # starts a comment.")
        self.rules.setMinimumHeight(120)
        apply_rules = QPushButton("Use these rules")
        apply_rules.clicked.connect(self.apply_rules)
        settings = well(row(muted("Toggle"), self.toggle, None), row(muted("Glows"), self.on_color, muted("on"), self.off_color, muted("off"), None),
                        row(muted("Axes move from"), self.deadzone, muted("to"), self.full, None), row(muted("Curve"), self.curve, None),
                        self.type_too, row(None, apply_settings),
                        muted("Rules, for typing it all at once"), self.rules, row(None, apply_rules), title="Settings")

        columns = QHBoxLayout()
        columns.setSpacing(10)
        columns.addWidget(roles, 3)
        columns.addWidget(current, 3)
        columns.addWidget(settings, 3)
        root.addLayout(columns, 1)
        self.status = StatusLine()
        root.addWidget(self.status)
        self.selection_changed()

    # ---- showing ----
    def loaded(self, config, running, sensor_keys):
        self.config, self.running = dict(config), running
        self.sensor_keys = {k for k in sensor_keys if k != "SS"}
        for name, cap in self.caps.items():
            live = name in self.sensor_keys
            cap.inert = not live
            cap.setEnabled(live)
            if not live:
                cap.setChecked(False)
        self.show_caps()
        self.show_table()
        self.toggle.setCurrentIndex(max(0, self.toggle.findData(config.get("toggle"))))
        self.on_color.set_color(config["toggle_on"])
        self.off_color.set_color(config["toggle_off"])
        self.deadzone.setValue(config["deadzone"])
        self.full.setValue(config["full"])
        self.curve.setValue(config["curve"])
        self.type_too.setChecked(bool(config.get("type_too")))
        self.rules.setPlainText(controller_rules(config["keys"]))
        keys = config["keys"]
        self.state.setText("Running." + (f" SteelSeries + {config['toggle']} turns it off and on." if config.get("toggle") else
                                         " Pause turns it off and on.") if running else ("Not running." if keys else "Nothing set up yet."))
        self.run_button.setText("Stop" if running else "Start")
        self.run_button.setEnabled(running or bool(keys))
        self.selection_changed()

    def show_caps(self):
        keys = self.config["keys"]
        for name, cap in self.caps.items():
            out = keys.get(name)
            cap.set_detail(controller_short(out) if out else "", fill=None, marked=bool(out))
            cap.setToolTip(controller_label(out) if out else "")

    def show_table(self):
        keys = self.config["keys"]
        self.table.blockSignals(True)
        self.table.setRowCount(len(keys))
        for r, (key, out) in enumerate(sorted(keys.items(), key=lambda kv: (axis_of(kv[1]) or (0, ""), kv[0]))):
            name = QTableWidgetItem(KEY_LABELS.get(key, key))
            name.setData(Qt.UserRole, key)
            self.table.setItem(r, 0, name)
            self.table.setItem(r, 1, QTableWidgetItem(controller_label(out)))
            remove = QPushButton("Remove")
            remove.clicked.connect(lambda checked=False, k=key: self.remove(k))
            self.table.setCellWidget(r, 2, remove)
        self.table.resizeColumnToContents(0)
        self.table.resizeColumnToContents(2)
        self.table.blockSignals(False)

    def selected(self):
        return [n for n, cap in self.caps.items() if cap.isChecked()]

    def selection_changed(self, *_):
        chosen = self.selected()
        if not chosen:
            text = "Pick keys on the keyboard"
        elif len(chosen) <= 6:
            text = "Picked " + " ".join(KEY_LABELS.get(n, n) for n in chosen)
        else:
            text = f"{len(chosen)} keys picked"
        self.picked_label.setText(text)
        for button in (*self.role_buttons, self.own_axis):
            button.setEnabled(bool(chosen))
        self.shared_axis.setEnabled(len(chosen) == 2)
        self.type_again.setEnabled(any(n in self.config["keys"] for n in chosen))

    def table_picked(self):
        wanted = {self.table.item(i.row(), 0).data(Qt.UserRole) for i in self.table.selectedIndexes()}
        for name, cap in self.caps.items():
            if not cap.inert:
                cap.setChecked(name in wanted)
        self.selection_changed()

    def clear_picks(self):
        for cap in self.caps.values():
            cap.setChecked(False)
        self.selection_changed()

    # ---- changing ----
    def settings(self):
        return {"toggle": self.toggle.currentData(), "toggle_on": self.on_color.color, "toggle_off": self.off_color.color,
                "deadzone": round(self.deadzone.value(), 1), "full": round(self.full.value(), 1), "curve": round(self.curve.value(), 1),
                "type_too": self.type_too.isChecked()}

    def submit(self, mapping, message, run=True):
        settings = self.settings()
        if mapping and settings["full"] <= settings["deadzone"]:
            self.status.say("Axes have to move to a point deeper than where they start.", "bad")
            return False
        if settings["toggle"] and settings["toggle"] in mapping:
            self.status.say(f"{settings['toggle']} is the toggle key, so it can't be on the controller too. Pick another toggle.", "bad")
            return False
        config = {**self.config, **settings, "keys": mapping}

        def done(_):
            self.status.say(message)
            self.changed()
        self.runner.submit(lambda: self.device.set_controller(config, run and bool(mapping)), done, lambda e: self.status.say(str(e), "bad"))
        return True

    def assign(self, out):
        names = self.selected()
        if not names:
            return
        if self.submit({**self.config["keys"], **{n: out for n in names}},
                       f"{' '.join(KEY_LABELS.get(n, n) for n in names[:6])}: {controller_label(out)}."):
            self.clear_picks()

    def assign_own_axes(self):
        names = self.selected()
        try:
            outs = next_axes([o for k, o in self.config["keys"].items() if k not in names], len(names))
        except ValueError as e:
            self.status.say(str(e), "bad")
            return
        if self.submit({**self.config["keys"], **dict(zip(names, outs))}, f"{len(names)} key{'s' if len(names) != 1 else ''}, an axis each."):
            self.clear_picks()

    def assign_shared_axis(self):
        names = self.selected()
        if len(names) != 2:
            return
        try:
            number = axis_of(next_axes([o for k, o in self.config["keys"].items() if k not in names], 1)[0])[0]
        except ValueError as e:
            self.status.say(str(e), "bad")
            return
        top, bottom = names  # the keyboard's order: the upper key, or the left one on the same row, comes first
        mapping = {**self.config["keys"], top: f"axis:{number}+", bottom: f"axis:{number}-"}
        if self.submit(mapping, f"Axis {number}: {KEY_LABELS.get(top, top)} moves it up, {KEY_LABELS.get(bottom, bottom)} down, "
                                f"and it rests in the middle."):
            self.clear_picks()

    def unassign(self):
        names = [n for n in self.selected() if n in self.config["keys"]]
        mapping = {k: v for k, v in self.config["keys"].items() if k not in names}
        if self.submit(mapping, f"{' '.join(KEY_LABELS.get(n, n) for n in names[:6])} type again."):
            self.clear_picks()

    def remove(self, key):
        self.submit({k: v for k, v in self.config["keys"].items() if k != key}, f"{KEY_LABELS.get(key, key)} types again.")

    def quick_setup(self, name):
        if name == "Clear all":
            self.submit({}, "Cleared: every key types again.")
            return
        layout = CONTROLLER_LAYOUTS[name]
        if layout == "every":
            toggle = self.toggle.currentData()
            keys = [k for k in self.caps if k in self.sensor_keys and k != toggle]
            self.submit(dict(zip(keys, next_axes([], len(keys)))),
                        f"Every key is its own axis: {len(keys)} of them, eight to a virtual device. Nothing types while it runs"
                        + (f": SteelSeries + {toggle} turns it off." if toggle else ". Pause turns it off, or set a toggle."))
            return
        self.submit({**{k: v for k, v in self.config["keys"].items() if k not in layout}, **layout}, f"{name}.")

    def apply_rules(self):
        try:
            mapping = parse_controller_rules(self.rules.toPlainText(), self.sensor_keys)
        except ValueError as e:
            self.status.say(str(e), "bad")
            return
        self.submit(mapping, f"Using {len(mapping)} rule{'s' if len(mapping) != 1 else ''}.")

    def toggle_running(self):
        if self.running:
            def done(_):
                self.status.say("Controller off: the keys type again.")
                self.changed()
            self.runner.submit(lambda: self.device.stop("map"), done, lambda e: self.status.say(str(e), "bad"))
        elif self.config["keys"]:
            self.submit(dict(self.config["keys"]), "Controller on.")


class HeadsetPage(QWidget):
    """The Arctis 9: what the headset is doing, and its settings. Each change is sent and read back straight away."""
    POLL_MS = 5000

    def __init__(self, device, runner, confirm=ask, parent=None):
        super().__init__(parent)
        self.setObjectName("page")
        self.device, self.runner, self.confirm = device, runner, confirm
        self.loading = False
        self.data = {}
        self.poller = QTimer(self, interval=self.POLL_MS)
        self.poller.timeout.connect(self.poll)
        self.build()

    def showEvent(self, event):
        super().showEvent(event)
        self.poller.start()

    def hideEvent(self, event):
        super().hideEvent(event)
        self.poller.stop()

    def build(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(14, 12, 14, 12)
        root.setSpacing(10)
        self.connection = QLabel("Looking for the headset")
        self.connection.setObjectName("title")
        self.firmware = muted("")
        self.retry = QPushButton("Try again")
        self.retry.clicked.connect(self.refresh)
        self.retry.hide()
        root.addWidget(header(self.connection, self.firmware, None, self.retry))
        self.content = QWidget()
        body = QHBoxLayout(self.content)
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(10)
        body.addWidget(self.build_status(), 2)
        body.addWidget(self.build_settings(), 3)
        body.addWidget(self.build_eq(), 5)
        root.addWidget(self.content, 1)
        self.status = StatusLine()
        self.save_button = QPushButton("Save to transmitter")
        self.save_button.setObjectName("primary")
        self.save_button.clicked.connect(self.save)
        root.addLayout(row(self.status, self.save_button))

    def build_status(self):
        self.link = QLabel("")
        self.link.setWordWrap(True)
        self.battery = muted("")
        self.chatmix = muted("")
        self.mic_state = muted("")
        panel = well(self.link, self.battery, self.chatmix, self.mic_state, title="Headset")
        panel.layout().addStretch(1)
        return panel

    def build_settings(self):
        self.sidetone = QComboBox()
        for label, value in SIDETONE_LEVELS:
            self.sidetone.addItem(label, value)
        self.sidetone.activated.connect(self.sidetone_picked)
        self.mic = QSlider(Qt.Horizontal)
        self.mic.setRange(arctis.MIC_DB.start, arctis.MIC_DB.stop - 1)
        self.mic_label = QLabel()
        self.mic_label.setFixedWidth(52)
        self.mic.valueChanged.connect(lambda v: self.mic_label.setText(f"{v} dB"))
        self.mic.sliderReleased.connect(self.mic_picked)
        self.surround = QCheckBox("Surround")
        self.surround.clicked.connect(self.surround_picked)
        self.sleep = QComboBox()
        for minutes in SLEEP_MINUTES:
            self.sleep.addItem("Never" if minutes == 0 else f"After {minutes} min", minutes)
        self.sleep.activated.connect(self.sleep_picked)
        hint = muted("Changes apply right away. Save to transmitter keeps them after it loses power.")
        hint.setWordWrap(True)
        panel = well(row(muted("Sidetone"), self.sidetone), row(muted("Mic gain"), self.mic, self.mic_label), self.surround,
                     row(muted("Turn off when silent"), self.sleep), hint, title="Settings")
        panel.layout().addStretch(1)
        return panel

    def build_eq(self):
        self.preset = QComboBox()
        for label, number in EQ_PRESET_LABELS:
            self.preset.addItem(label, number)
        self.preset.activated.connect(self.preset_picked)
        bands = QHBoxLayout()
        self.bands = []
        for name in arctis.EQ_BANDS:
            slider = QSlider(Qt.Vertical)
            slider.setRange(-100, 100)  # tenths of a dB
            slider.setMinimumHeight(140)
            label = muted("+0.0")
            slider.valueChanged.connect(lambda v, l=label: l.setText(f"{v / 10:+.1f}"))
            column = QVBoxLayout()
            column.addWidget(label, 0, Qt.AlignHCenter)
            column.addWidget(slider, 1, Qt.AlignHCenter)
            column.addWidget(muted(name), 0, Qt.AlignHCenter)
            bands.addLayout(column)
            self.bands.append(slider)
        self.use_bands = QPushButton("Use these bands")
        self.use_bands.clicked.connect(self.bands_picked)
        panel = well(row(muted("Preset"), self.preset, None), bands, row(None, self.use_bands), title="Equalizer")
        return panel

    # Loading

    def refresh(self):
        self.runner.submit(self.device.read, self.loaded, self.lost)

    def poll(self):
        if self.data:
            self.runner.submit(self.device.status, self.show_status, lambda e: None)

    def loaded(self, data):
        self.loading = True
        self.data = data
        self.retry.hide()
        self.content.setEnabled(True)
        self.save_button.setEnabled(True)
        self.connection.setText("Arctis 9 Wireless")
        self.firmware.setText(f"transmitter {data['versions']['transmitter']}, headset {data['versions']['headset']}")
        self.show_status(data["status"])
        level = arctis.SIDETONE_NAMES.get(data["sidetone"])
        self.sidetone.setCurrentIndex(max(0, self.sidetone.findData(level)))
        self.mic.setValue(data["mic"])
        self.mic_label.setText(f"{data['mic']} dB")
        self.surround.setChecked(data["surround"])
        minutes = data["sleep"] // 60
        index = self.sleep.findData(minutes)
        if index < 0:
            self.sleep.addItem(f"After {minutes} min", minutes)
            index = self.sleep.count() - 1
        self.sleep.setCurrentIndex(index)
        self.preset.setCurrentIndex(max(0, self.preset.findData(data["preset"])))
        for slider, db in zip(self.bands, data["bands"]):
            slider.setValue(round(db * 10))
        self.loading = False
        if not self.status.text():
            self.status.say("Changes apply right away. Save to keep them after the transmitter loses power.")

    def show_status(self, status):
        if not status.get("ready"):
            self.link.setText("The transmitter is starting up.")
            return
        if not status["connected"]:
            self.link.setText("The headset isn't connected. Turn it on, or bring it closer.")
            self.battery.setText("")
        else:
            self.link.setText("Connected")
            self.battery.setText(f"Battery {status['battery_mv'] / 1000:.2f} V")
        self.chatmix.setText(f"ChatMix: chat {status['chat']}, game {status['game']}")
        self.mic_state.setText("Mic muted" if status["mic_muted"] else "Mic on")

    def lost(self, error):
        self.data = {}
        self.content.setEnabled(False)
        self.save_button.setEnabled(False)
        self.connection.setText("Headset not reachable")
        self.firmware.setText("")
        self.retry.show()
        self.status.say(str(error), "bad")

    # Applying

    def apply(self, method, *args, message):
        def done(_):
            self.status.say(message)
            self.refresh()

        self.runner.submit(lambda: self.device.call(method, *args), done, lambda e: self.status.say(str(e), "bad"))

    def sidetone_picked(self, *_):
        if not self.loading:
            level = self.sidetone.currentData()
            self.apply("set_sidetone", arctis.SIDETONE[level], message=f"Sidetone: {level}.")

    def mic_picked(self):
        if not self.loading:
            self.apply("set_mic", self.mic.value(), message=f"Mic gain: {self.mic.value()} dB.")

    def surround_picked(self, on):
        if not self.loading:
            self.apply("set_surround", on, message=f"Surround {'on' if on else 'off'}.")

    def sleep_picked(self, *_):
        if not self.loading:
            minutes = self.sleep.currentData()
            self.apply("set_sleep", minutes * 60, message="The headset never turns itself off." if not minutes else
                       f"The headset turns off after {minutes} min without sound.")

    def preset_picked(self, *_):
        if not self.loading:
            self.apply("set_eq_preset", self.preset.currentData(), message=f"EQ: {self.preset.currentText()}.")

    def bands_picked(self):
        dbs = [slider.value() / 10 for slider in self.bands]

        def both():
            self.device.call("set_eq_bands", dbs)
            self.device.call("set_eq_preset", arctis.EQ_PRESETS["custom"])

        def done(_):
            self.status.say("EQ: your own bands.")
            self.refresh()

        self.runner.submit(both, done, lambda e: self.status.say(str(e), "bad"))

    def save(self):
        if self.confirm(self, "Save to transmitter", "Write the current settings to the transmitter's flash, so they come back after it loses power?",
                        "Save to transmitter"):
            self.apply("save", message="Saved to the transmitter.")


class GearDevices:
    """Every connected SteelSeries device, and the general engine for the ones a definition covers."""

    def scan(self):
        return [d for d in steelmc_devices.scan() if d["part"] != "bootloader"]

    def families(self):
        return steelmc_devices.families()

    def status(self, pid):
        with gear.Gear(pid, self.families()) as g:
            return {"status": g.status(), "battery": g.battery(), "firmware": g.firmware()}

    def set(self, pid, name, value):
        with gear.Gear(pid, self.families()) as g:
            g.set(name, value)

    def save(self, pid):
        with gear.Gear(pid, self.families()) as g:
            g.save()

    def remembered(self, pid):
        return gear.remembered(pid)


OWN_TABS = {"mouse": "Mouse", "keyboard": "Keyboard", "headset": "Headset"}
SETTING_HINTS = {"dpi_stages": "400,800,1600", "buttons": "button4=f13,button5=disable", "gradient": "2000ms:red@0,blue@50,red@100",
                 "gradient2": "2000ms:red@0,blue@50,red@100", "eq": "0,0,0,0,0,0,0,0,0,0", "zones": "ff8000, or one colour per zone",
                 "per_key": "every key, then KEY=VALUE: 1.5,w=0.5"}


class DevicesPage(QWidget):
    """Every other SteelSeries device plugged in, with the settings its definition offers. Changes there are experimental."""

    def __init__(self, devices, runner, confirm=ask, parent=None):
        super().__init__(parent)
        self.setObjectName("page")
        self.devices, self.runner, self.confirm = devices, runner, confirm
        self.found, self.covered, self.current = [], {}, None
        self.inputs = {}
        root = QVBoxLayout(self)
        root.setContentsMargins(14, 12, 14, 12)
        root.setSpacing(10)
        self.title = QLabel("Other devices")
        self.title.setObjectName("title")
        rescan = QPushButton("Look again")
        rescan.clicked.connect(self.refresh)
        root.addWidget(header(self.title, None, rescan))
        body = QHBoxLayout()
        body.setSpacing(10)
        self.list = QVBoxLayout()
        self.list_panel = well(title="Plugged in")
        self.list_panel.layout().addLayout(self.list)
        self.list_panel.layout().addStretch(1)
        body.addWidget(self.list_panel, 2)
        self.detail = well()
        body.addWidget(self.detail, 5)
        root.addLayout(body, 1)
        self.status = StatusLine()
        root.addWidget(self.status)
        self.show_device(None)

    def refresh(self):
        def load():
            return self.devices.scan(), self.devices.families()
        self.runner.submit(load, self.loaded, lambda e: self.status.say(str(e), "bad"))

    def loaded(self, result):
        self.found, self.covered = result
        self.empty(self.list)
        if not self.found:
            self.list.addWidget(muted("No SteelSeries devices found."))
        for d in self.found:
            name = d["product"] or d["name"] or d["id"]
            pid = d["id"].split(":")[1]
            button = QPushButton(name)
            button.setToolTip(d["id"])
            button.clicked.connect(lambda checked=False, dev=d: self.show_device(dev))
            self.list.addWidget(button)
            note = self.support_note(d, pid)
            label = muted(note)
            label.setWordWrap(True)
            self.list.addWidget(label)
        if self.current and any(d["id"] == self.current["id"] for d in self.found):
            self.show_device(next(d for d in self.found if d["id"] == self.current["id"]))

    def support_note(self, d, pid):
        if pid in self.covered:
            return "Settings here, experimental"
        if d["support"] in ("full", "experimental", "read-only") and d["kind"] in OWN_TABS:
            return f"On the {OWN_TABS[d['kind']]} tab"
        if d["support"] == "part of a supported device":
            return "Part of a device above"
        return "Not supported yet"

    @staticmethod
    def empty(layout):
        """Take everything out, hiding it straight away: deleteLater leaves a widget on screen until the next event loop pass."""
        while layout.count():
            item = layout.takeAt(0)
            if item.widget():
                item.widget().hide()
                item.widget().deleteLater()
            elif item.layout():
                DevicesPage.empty(item.layout())

    def clear_detail(self):
        self.empty(self.detail.layout())
        self.inputs = {}

    def show_device(self, d):
        self.clear_detail()
        self.current = d
        layout = self.detail.layout()
        if d is None:
            layout.addWidget(muted("Pick a device on the left."))
            layout.addStretch(1)
            return
        pid = d["id"].split(":")[1]
        title = QLabel(d["product"] or d["name"] or d["id"])
        title.setObjectName("title")
        layout.addWidget(title)
        if pid not in self.covered:
            note = self.support_note(d, pid)
            text = (f"{note}." if note.startswith("On the") else
                    "This one isn't supported yet. steelmc-devices probe in a terminal prints what's needed to add it.")
            layout.addWidget(muted(text))
            layout.addStretch(1)
            return
        family, product = self.covered[pid]
        checked = product.get("firmware_checked")
        warning = muted("Changing settings here is experimental: nobody has tried them on a real one yet. "
                        + (f"Every command is in this model's firmware {checked}." if checked else
                           "Its commands haven't been matched against its own firmware yet."))
        warning.setWordWrap(True)
        layout.addWidget(warning)
        self.readout = muted("")
        self.readout.setWordWrap(True)
        layout.addWidget(self.readout)
        if family.get("status") or family.get("battery") or family.get("firmware"):
            read = QPushButton("Read battery and status")
            read.clicked.connect(lambda: self.read_status(pid))
            layout.addLayout(row(read, None))
        last = self.devices.remembered(pid)
        grid = QGridLayout()
        grid.setHorizontalSpacing(8)
        for i, (name, s) in enumerate(family["settings"].items()):
            if s["type"].startswith("unsupported"):
                continue
            label = muted(s.get("label") or name)
            widget = self.input_for(s, last.get(name))
            apply = QPushButton("Do it" if s["type"] == "action" else "Set")
            apply.clicked.connect(lambda checked=False, n=name, w=widget, t=s["type"]: self.apply(pid, n, w, t))
            grid.addWidget(label, i, 0)
            if widget is not None:
                grid.addWidget(widget, i, 1)
            grid.addWidget(apply, i, 2)
            self.inputs[name] = widget
        layout.addLayout(grid)
        if family.get("save"):
            save = QPushButton("Save to device")
            save.setObjectName("primary")
            save.clicked.connect(lambda: self.save(pid))
            layout.addLayout(row(None, save))
        layout.addStretch(1)

    def input_for(self, s, last):
        kind = s["type"]
        if kind == "choice":
            box = QComboBox()
            box.addItems([str(k) for k in s["choices"]])
            if last is not None and box.findText(str(last)) >= 0:
                box.setCurrentText(str(last))
            return box
        if kind in ("number", "number_choice"):
            spin = QSpinBox()
            spin.setRange(s["input"][0], s["input"][1])
            spin.setSingleStep(max(1, s["input"][2] if len(s["input"]) > 2 else 1))
            spin.setValue(int(last) if last is not None else int(s.get("default", s["input"][0]) or s["input"][0]))
            return spin
        if kind in ("color", "reactive_color"):
            edit = QLineEdit(str(last or s.get("default", "") or ""))
            edit.setPlaceholderText("RRGGBB or a name" + (", or off" if kind == "reactive_color" else ""))
            return edit
        if kind == "eq" and s.get("presets"):
            box = QComboBox()
            box.setEditable(True)
            box.addItems(list(s["presets"]))
            box.lineEdit().setPlaceholderText(SETTING_HINTS["eq"])
            if last:
                box.setCurrentText(str(last))
            return box
        if kind == "per_key" and s.get("value") != "color":
            box = QComboBox()  # one value for every key; type KEY=VALUE after it for keys that differ
            box.setEditable(True)
            box.addItems([str(v) for v in s["values"]])
            box.lineEdit().setPlaceholderText(SETTING_HINTS["per_key"])
            box.setCurrentText(str(last if last is not None else s.get("default", "")))
            box.setToolTip("Pick one value for every key, or type it followed by KEY=VALUE for keys that differ, like 1.5,w=0.5,a=0.5")
            return box
        if kind in ("per_key", "zones"):
            edit = QLineEdit(str(last or ""))
            edit.setPlaceholderText("RRGGBB or a name" + (", then KEY=COLOR" if kind == "per_key" else ", or one per zone"))
            return edit
        if kind == "action":
            return None
        edit = QLineEdit(str(last or ""))
        edit.setPlaceholderText(SETTING_HINTS.get(kind, ""))
        return edit

    @staticmethod
    def value_of(widget, kind):
        if widget is None:
            return None
        if isinstance(widget, QComboBox):
            return widget.currentText()
        if isinstance(widget, QSpinBox):
            return str(widget.value())
        return widget.text().strip()

    def apply(self, pid, name, widget, kind):
        value = self.value_of(widget, kind)
        if kind != "action" and not value:
            self.status.say("Say what to set it to first.", "bad")
            return
        self.runner.submit(lambda: self.devices.set(pid, name, value),
                           lambda _: self.status.say(f"Sent {name}{f' = {value}' if value else ''}. If it didn't take, please report it."),
                           lambda e: self.status.say(str(e), "bad"))

    def read_status(self, pid):
        def shown(report):
            status, parts = report.get("status") or {}, []
            if status.get("connected") is False:
                parts.append("not connected")
            battery = report.get("battery") or ({"percent": status["battery"]} if status.get("battery") is not None else None)
            if battery and status.get("connected") is not False:
                parts.append(f"battery {battery['percent']}%" + (", charging" if battery.get("charging") or status.get("charging") else ""))
            for side in ("left_battery", "right_battery"):
                if status.get(side) is not None:
                    parts.append(f"{side.split('_')[0]} bud {status[side]}%")
            if status.get("chat") is not None:
                parts.append(f"ChatMix game {status.get('game')}%, chat {status['chat']}%")
            if report.get("firmware"):
                parts.append(f"firmware {report['firmware']}")
            self.readout.setText(", ".join(parts) or "It didn't report anything.")

        self.runner.submit(lambda: self.devices.status(pid), shown, lambda e: self.status.say(str(e), "bad"))

    def save(self, pid):
        if self.confirm(self, "Save to device", "Write the current settings to the device's memory, so they last after it loses power?", "Save"):
            self.runner.submit(lambda: self.devices.save(pid), lambda _: self.status.say("Saved."), lambda e: self.status.say(str(e), "bad"))


class NoDevices:
    """For a window made with other devices passed in: it never looks for real ones."""

    def scan(self):
        return []

    def families(self):
        return {}

    def remembered(self, pid):
        return {}


class NoHeadset:
    """For a window made with other devices passed in: it never reaches for a real headset."""

    def read(self):
        raise arctis.ArctisError("No Arctis 9 transmitter found. Is it plugged in?")

    def status(self):
        return self.read()

    def call(self, *_):
        return self.read()


class MainWindow(QMainWindow):
    def __init__(self, mouse=None, keyboard=None, runners=None, confirm=ask, oled=None, headset=None, devices=None):
        super().__init__()
        self.setWindowTitle("SteelMC")
        runners = runners or (Runner(), Runner(), Runner(), Runner())
        self.mouse_page = MousePage(mouse or MouseDevice(), runners[0], confirm)
        self.keyboard_page = KeyboardPage(keyboard or KeyboardDevice(), runners[1], confirm, oled=oled or (OledDevice() if keyboard is None else None),
                                          oled_runner=runners[-1])
        tabs = QTabWidget()
        tabs.setDocumentMode(True)
        tabs.tabBar().setDrawBase(False)
        real = mouse is None and keyboard is None
        self.headset_page = HeadsetPage(headset or (HeadsetDevice() if real else NoHeadset()), runners[3] if len(runners) > 3 else runners[0],
                                        confirm)
        tabs.addTab(self.mouse_page, "Mouse")
        tabs.addTab(self.keyboard_page, "Keyboard")
        self.devices_page = DevicesPage(devices or (GearDevices() if real else NoDevices()), runners[3] if len(runners) > 3 else runners[0], confirm)
        tabs.addTab(self.headset_page, "Headset")
        tabs.addTab(self.devices_page, "Other devices")
        self.setCentralWidget(tabs)
        self.tabs = tabs
        self.setMinimumSize(QSize(1100, 800))

    def load(self):
        self.mouse_page.refresh()
        self.keyboard_page.refresh()
        self.headset_page.refresh()
        self.devices_page.refresh()

    def closeEvent(self, event):
        self.keyboard_page.stop_stream()
        super().closeEvent(event)


def arrow_images():
    """QSS needs image files for arrows, so draw hard little triangles into a temp folder."""
    folder = tempfile.mkdtemp(prefix="steelmc-")
    atexit.register(shutil.rmtree, folder, ignore_errors=True)
    paths = {}
    shapes = {"DOWN": ((0, 0), (9, 0), (4.5, 6)), "UP": ((0, 6), (9, 6), (4.5, 0))}
    for name, points in shapes.items():
        for suffix, color in (("", MUTED), ("_OFF", "#71767c")):
            image = QImage(9, 6, QImage.Format_ARGB32)
            image.fill(Qt.transparent)
            p = QPainter(image)
            p.setRenderHint(QPainter.Antialiasing)
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(color))
            p.drawPolygon(QPolygonF([QPointF(*point) for point in points]))
            p.end()
            path = os.path.join(folder, f"{name}{suffix}.png")
            image.save(path)
            paths[f"@@{name}{suffix}@@"] = path
    return paths


def style_app(app):
    """Fusion and a fixed palette, so the app looks the same on every desktop."""
    app.setStyle("Fusion")
    palette = QPalette()
    for role, color in ((QPalette.Window, CHASSIS), (QPalette.WindowText, TEXT), (QPalette.Base, WELL), (QPalette.AlternateBase, CAP),
                        (QPalette.Text, TEXT), (QPalette.Button, CAP), (QPalette.ButtonText, TEXT), (QPalette.Highlight, "#2f53c9"),
                        (QPalette.HighlightedText, "#ffffff"), (QPalette.PlaceholderText, "#71767c"), (QPalette.ToolTipBase, WELL),
                        (QPalette.ToolTipText, TEXT)):
        palette.setColor(role, QColor(color))
    app.setPalette(palette)
    sheet = STYLE
    for marker, path in arrow_images().items():
        sheet = sheet.replace(marker, path)
    app.setStyleSheet(sheet)


def main(argv=None):
    app = QApplication(argv if argv is not None else sys.argv)
    app.setApplicationName("SteelMC")
    app.setDesktopFileName("net.local.steelmc")
    style_app(app)
    window = MainWindow()
    window.show()
    window.load()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
