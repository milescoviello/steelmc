#!/usr/bin/env python3
"""SteelSeries settings: one window for the Aerox 9 Wireless and the Apex Pro Gen 3, no GG needed.

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
from PySide6.QtGui import QColor, QFont, QImage, QPainter, QPalette, QPen, QPolygonF
from PySide6.QtWidgets import (
    QAbstractButton, QApplication, QButtonGroup, QCheckBox, QColorDialog, QComboBox, QFrame, QGridLayout, QHBoxLayout,
    QInputDialog, QLabel, QLineEdit, QMainWindow, QMessageBox, QPushButton, QRadioButton, QRubberBand, QSizePolicy, QSlider,
    QSpinBox,
    QStackedWidget, QTabWidget, QVBoxLayout, QWidget,
)

import aerox

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
# Keys the keyboard keeps for its own settings while its SteelSeries key is held, plus that key itself.
SHORTCUT_TAKEN = {"Q", "I", "O", "T", "LGUI", "SS"}
SHORTCUT_KINDS = [("Press keys", "keys", "ctrl+c"), ("Type text", "text", "hello"), ("Run a command", "command", "firefox"),
                  ("Volume up", "volume+", ""), ("Volume down", "volume-", ""), ("Brightness up", "brightness+", ""),
                  ("Brightness down", "brightness-", "")]
FEATURES = [
    ("gamepad", "W, A, S, D as an analog controller", "While this is on, those keys stop typing."),
    ("oled", "Key depth on the keyboard's screen", "Shows how far each key is pressed on the small display."),
]


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
            except (aerox.AeroxError, KeyboardError, OSError) as e:
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
        return {"info": info, "jobs": parse_jobs(status), "presets": presets, "shortcuts": self.shortcuts()}

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

    def lights(self, args, color=None):
        if args is None:
            return self.run("lights", "--off")
        return self.run(*args, *(["--color", color] if color else []))

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
        self.kind = QComboBox()
        for label, value in ACTION_KINDS:
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
        self.kind_fields.addWidget(self.keys_edit)
        self.kind_fields.addWidget(self.mouse_number)
        self.kind_fields.addWidget(self.media_key)
        self.kind_fields.addWidget(QWidget())
        self.kind_fields.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.kind_fields.setFixedHeight(32)
        self.bind_button = QPushButton("Bind")
        self.bind_button.clicked.connect(self.bind)
        self.default_button = QPushButton("Reset to default")
        self.default_button.clicked.connect(self.reset_button)
        self.bind_button.setEnabled(False)
        self.default_button.setEnabled(False)
        return well(self.editing, row(self.kind, self.kind_fields), row(None, self.default_button, self.bind_button))

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
        if action.startswith("mouse"):
            kind = "mouse"
            self.mouse_number.setCurrentIndex(self.mouse_number.findData(action))
        elif action.startswith("media:"):
            kind = "media"
            self.media_key.setCurrentIndex(max(0, self.media_key.findData(action)))
        elif action in ("dpi", "scroll-up", "scroll-down", "tilt-left", "tilt-right", "none"):
            kind = action
        else:
            kind = "keys"
            self.keys_edit.setCurrentText(action)
        self.kind.setCurrentIndex(self.kind.findData(kind))
        self.show_kind()

    def show_kind(self, *_):
        kind = self.kind.currentData()
        self.kind_fields.setCurrentIndex({"keys": 0, "mouse": 1, "media": 2}.get(kind, 3))

    def action_text(self):
        kind = self.kind.currentData()
        if kind == "keys":
            return self.keys_edit.currentText()
        if kind == "mouse":
            return self.mouse_number.currentData()
        if kind == "media":
            return self.media_key.currentData()
        return kind

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
        controls.addWidget(self.build_actuation(), 5)
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
        self.selection_changed()
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
        hint = muted("Hold the SteelSeries key and press another key. Red keys are the keyboard's own.")
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
        self.connection.setText("Apex Pro Gen 3")
        self.firmware.setText(f"firmware {info['firmware']}, {len(info['keys'])} keys with sensors")
        self.settings = info["keys"]
        for name, cap in self.caps.items():
            key = self.settings.get(name)
            if key:
                cap.inert = False
                cap.setEnabled(True)
                cap.setFocusPolicy(Qt.StrongFocus)
                mm = self.key_depth(key)
                rapid = key["mode"] != 0
                cap.fires_at = key["press"]
                shortcut = data.get("shortcuts", {}).get(name)
                cap.set_detail(f"{mm:.1f}" + (" R" if rapid else ""), fill=key["press"] / TRAVEL_MM, marked=rapid or bool(shortcut))
                cap.setToolTip(f"Fires at {mm:.1f} mm" + (", rapid tap on" if rapid else "") + (f". With the SteelSeries key: {shortcut}" if shortcut else ""))
            else:
                cap.inert = True
                cap.setEnabled(False)
                cap.setChecked(False)
        current = self.preset.currentText()
        self.preset.clear()
        self.preset.addItems(data["presets"])
        if current in data["presets"]:
            self.preset.setCurrentText(current)
        self.show_jobs(data["jobs"])
        self.shortcuts = data.get("shortcuts", {})
        self.shortcut_list.setText("\n".join(f"{KEY_LABELS.get(k, k)}: {v}" for k, v in sorted(self.shortcuts.items())))
        self.loading = False
        self.selection_changed()
        if not self.status.text():
            self.status.say("Changes apply right away. Save to keep them after a replug.")

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

    def shortcut_target(self):
        """The one picked key a shortcut can go on, or None."""
        chosen = self.selected_keys()
        return chosen[0] if len(chosen) == 1 and chosen[0] not in SHORTCUT_TAKEN else None

    def show_shortcut_key(self):
        if not hasattr(self, "shortcut_save"):
            return
        chosen, target = self.selected_keys(), self.shortcut_target()
        if target:
            self.shortcut_key.setText(f"SteelSeries + {KEY_LABELS.get(target, target)}")
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


class MainWindow(QMainWindow):
    def __init__(self, mouse=None, keyboard=None, runners=None, confirm=ask, oled=None):
        super().__init__()
        self.setWindowTitle("SteelSeries settings")
        runners = runners or (Runner(), Runner(), Runner())
        self.mouse_page = MousePage(mouse or MouseDevice(), runners[0], confirm)
        self.keyboard_page = KeyboardPage(keyboard or KeyboardDevice(), runners[1], confirm, oled=oled or (OledDevice() if keyboard is None else None),
                                          oled_runner=runners[-1])
        tabs = QTabWidget()
        tabs.setDocumentMode(True)
        tabs.tabBar().setDrawBase(False)
        tabs.addTab(self.mouse_page, "Mouse")
        tabs.addTab(self.keyboard_page, "Keyboard")
        self.setCentralWidget(tabs)
        self.tabs = tabs
        self.setMinimumSize(QSize(1100, 800))

    def load(self):
        self.mouse_page.refresh()
        self.keyboard_page.refresh()

    def closeEvent(self, event):
        self.keyboard_page.stop_stream()
        super().closeEvent(event)


def arrow_images():
    """QSS needs image files for arrows, so draw hard little triangles into a temp folder."""
    folder = tempfile.mkdtemp(prefix="aerox-gui-")
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
    app.setApplicationName("SteelSeries settings")
    app.setDesktopFileName("net.local.aerox-gui")
    style_app(app)
    window = MainWindow()
    window.show()
    window.load()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
