#!/usr/bin/env python3
"""apex-analog: how far every key is pressed on a SteelSeries Apex Pro Gen 3, on Linux, wired to anything.

  apex-analog                   live bars for whatever you press
  apex-analog gamepad           W S A D become throttle, brake and steering on a virtual controller
  apex-analog map W=rt A=lx-    any key to a controller, pedals, the mouse, a keystroke, or a command
  apex-analog midi              play it as a MIDI instrument, with velocity and per-key pressure
  apex-analog type              press deep for capitals or key combos
  apex-analog lights            make keys glow with how far they are pressed
  apex-analog stream            one JSON line per update, for piping into anything
  apex-analog overlay           key pressure on your stream, as an OBS browser source
  apex-analog profile           how you type, and what it means for your actuation point
  apex-analog health            check every switch for drift, noise and short range
  apex-analog typeprint         experiment: recognise you by how you press keys
  apex-analog actuation W 1.0   set where keys actuate, no GG needed
  apex-analog info              the keyboard, your access, and every key's actuation settings

  from apex_analog import Keyboard
  with Keyboard() as kb:
      print(kb.read()["W"])     # millimetres

Only sends the read commands in ALLOWED, plus LIGHT_COMMANDS when you ask for lights and SETTING_COMMANDS when you set
actuation, and only to the firmware they were checked on. No dependencies.
"""
import argparse
import base64
import ctypes
import ctypes.util
import fcntl
import glob
import hashlib
import json
import math
import os
import re
import select
import signal
import socket
import statistics
import struct
import subprocess
import sys
import termios
import threading
import time
from typing import NamedTuple

__version__ = "0.5.0"

VENDOR, PRODUCT = "1038", "1640"
BOOTLOADER_PRODUCT = "1641"
FIRMWARE = "0416"  # bcdDevice of firmware 4.16.8, the only version these commands were read from

# Every command this program can send. Each handler was read in firmware 4.16.8 and only copies memory into the reply:
# no flash, no reset, no settings changed. Anything else is refused in Keyboard._ask, and there is no way to override it.
ALLOWED = {
    0xD7: "14 raw + 14 filtered sensor values for one bank of keys",
    0xDA: "each key's calibrated bottom-out and rest values",
    0xB1: "each key's press and release actuation points",
    0xB2: "each key's mode byte",
    0x9E: "whether the OLED's ACTUATION ICON is on",
}

# The only commands that change anything, sent only for lights. Each handler was read on both chips: they store colours in the
# lighting chip's memory and nothing else, so a replug or 3B undoes them. The rest of the lighting chip's commands stay out,
# including its own firmware update commands.
LIGHT_COMMANDS = {
    0x3A: "set the colour of up to 15 keys",
    0x3B: "go back to the keyboard's normal lighting",
}
# Every key the lighting chip has an LED for, sensor or not, from its own table.
LED_HIDS = [
    0x04, 0x05, 0x06, 0x07, 0x08, 0x09, 0x0A, 0x0B, 0x0C, 0x0D, 0x0E, 0x0F, 0x10, 0x11, 0x12, 0x13, 0x14, 0x15, 0x16, 0x17, 0x18, 0x19, 0x1A, 0x1B,
    0x1C, 0x1D, 0x1E, 0x1F, 0x20, 0x21, 0x22, 0x23, 0x24, 0x25, 0x26, 0x27, 0x28, 0x29, 0x2A, 0x2B, 0x2C, 0x2D, 0x2E, 0x2F, 0x30, 0x31, 0x32, 0x33,
    0x34, 0x35, 0x36, 0x37, 0x38, 0x39, 0x3A, 0x3B, 0x3C, 0x3D, 0x3E, 0x3F, 0x40, 0x41, 0x42, 0x43, 0x44, 0x45, 0x46, 0x47, 0x48, 0x49, 0x4A, 0x4B,
    0x4C, 0x4D, 0x4E, 0x4F, 0x50, 0x51, 0x52, 0x53, 0x54, 0x55, 0x56, 0x57, 0x58, 0x59, 0x5A, 0x5B, 0x5C, 0x5D, 0x5E, 0x5F, 0x60, 0x61, 0x62, 0x63,
    0x64, 0x85, 0x87, 0x88, 0x89, 0x8A, 0x8B, 0xE0, 0xE1, 0xE2, 0xE3, 0xE4, 0xE5, 0xE6, 0xE7, 0xF0, 0xFB,
]

# Changes a setting, only when you ask. Its handler stores in memory and reaches no flash save. It ignores LGUI (hid 0xE3).
SETTING_COMMANDS = {
    0x31: "set the press and release points of up to 20 keys",
}

# Sensor channel -> (HID usage, name, Linux key code), from the keyboard's own key table.
# ESC, the F row, nav keys and arrows are not here: they have no sensor. INT1-5, # and ISO\ exist only on JIS or ISO boards.
CHANNELS = {
    0: (0x35, "`", 41), 1: (0x1E, "1", 2), 2: (0x1F, "2", 3), 3: (0x20, "3", 4), 4: (0x21, "4", 5), 5: (0x22, "5", 6),
    6: (0x23, "6", 7), 7: (0x24, "7", 8), 8: (0x25, "8", 9), 9: (0x26, "9", 10), 10: (0x27, "0", 11), 11: (0x2D, "-", 12),
    12: (0x2E, "=", 13), 13: (0x89, "INT3", 124), 14: (0x2B, "TAB", 15), 15: (0x14, "Q", 16), 16: (0x1A, "W", 17),
    17: (0x08, "E", 18), 18: (0x15, "R", 19), 19: (0x17, "T", 20), 20: (0x1C, "Y", 21), 21: (0x18, "U", 22),
    22: (0x0C, "I", 23), 23: (0x12, "O", 24), 24: (0x13, "P", 25), 25: (0x2F, "[", 26), 26: (0x30, "]", 27),
    27: (0x31, "\\", 43), 28: (0x39, "CAPS", 58), 29: (0x04, "A", 30), 30: (0x16, "S", 31), 31: (0x07, "D", 32),
    32: (0x09, "F", 33), 33: (0x0A, "G", 34), 34: (0x0B, "H", 35), 35: (0x0D, "J", 36), 36: (0x0E, "K", 37),
    37: (0x0F, "L", 38), 38: (0x33, ";", 39), 39: (0x34, "'", 40), 40: (0x32, "#", 43), 41: (0x28, "ENTER", 28),
    42: (0xE1, "LSHIFT", 42), 43: (0x64, "ISO\\", 86), 44: (0x1D, "Z", 44), 45: (0x1B, "X", 45), 46: (0x06, "C", 46),
    47: (0x19, "V", 47), 48: (0x05, "B", 48), 49: (0x11, "N", 49), 50: (0x10, "M", 50), 51: (0x36, ",", 51),
    52: (0x37, ".", 52), 53: (0x38, "/", 53), 54: (0x87, "INT1", 89), 55: (0xE5, "RSHIFT", 54), 56: (0xE0, "LCTRL", 29),
    57: (0xE3, "LGUI", 125), 58: (0xE2, "LALT", 56), 59: (0x8B, "INT5", 94), 60: (0x2C, "SPACE", 57), 61: (0x8A, "INT4", 92),
    62: (0x88, "INT2", 93), 63: (0xE6, "RALT", 100), 64: (0xE7, "RGUI", 126), 65: (0xF0, "SS", None), 66: (0xE4, "RCTRL", 97),
    67: (0x2A, "BKSP", 14),
}
BY_NAME = {name: (channel, hid, code) for channel, (hid, name, code) in CHANNELS.items()}
ALIASES = {
    "SHIFT": "LSHIFT", "CTRL": "LCTRL", "ALT": "LALT", "WIN": "LGUI", "META": "LGUI", "SUPER": "LGUI", "RETURN": "ENTER",
    "BACKSPACE": "BKSP", "CAPSLOCK": "CAPS", "COMMA": ",", "PERIOD": ".", "SLASH": "/", "BACKSLASH": "\\", "SEMICOLON": ";",
    "QUOTE": "'", "GRAVE": "`", "TILDE": "`", "MINUS": "-", "EQUALS": "=", "LBRACKET": "[", "RBRACKET": "]",
}
NO_MAGNET = (2900, 928)  # calibration the firmware leaves on channels with no switch fitted
BANK_SIZE = 14
WRITE_GAP = 0.005  # seconds to leave after a command that gets no reply

# The firmware's own actuation levels: position as n/255 of a key's calibrated range -> distance in mm.
# The sensors are not linear: half the sensor range is already 3.0 mm.
CURVE = [(0, 0.0), (5, 0.2), (8, 0.4), (12, 0.6), (16, 0.8), (21, 1.0), (26, 1.2), (31, 1.4), (38, 1.6), (46, 1.8), (55, 2.0),
         (65, 2.2), (77, 2.4), (91, 2.6), (108, 2.8), (128, 3.0), (151, 3.2), (180, 3.4), (206, 3.7), (215, 4.0)]
TRAVEL_MM = 4.0
# The firmware's actuation levels: tenths of a mm per level, and the threshold list its press and release points come from.
LEVEL_TENTHS = [1, 2, 4, 6, 8, 10, 12, 14, 16, 18, 20, 22, 24, 26, 28, 30, 32, 34, 37, 40]
LEVEL_THRESHOLDS = [5, 5, 4, 5, 6, 8, 10, 12, 14, 16, 18, 21, 23, 26, 28, 31, 34, 38, 42, 46, 50, 55, 60, 65, 71, 77, 84, 91, 99, 108,
                    118, 128, 139, 151, 165, 180, 201, 206, 210, 215]
AT_REST_MM = 0.2  # below this a key counts as untouched: some keys rest up to 0.14 mm above their calibrated rest value


class ApexError(Exception):
    """Something the user can fix. The message says how."""


class NotFound(ApexError):
    pass


class AccessDenied(ApexError):
    pass


class Disconnected(ApexError):
    pass


def curve_mm(n):
    """Position as n/255 of the key's range -> millimetres."""
    if n <= 0:
        return 0.0
    for (x0, mm0), (x1, mm1) in zip(CURVE, CURVE[1:]):
        if n <= x1:
            return mm0 + (mm1 - mm0) * (n - x0) / (x1 - x0)
    return TRAVEL_MM


def actuation_level(mm):
    """Depth in mm -> (mm the keyboard calls it, press point, release point), at the nearest of the firmware's 20 levels."""
    if not 0.1 <= mm <= TRAVEL_MM:
        raise ValueError(f"actuation must be between 0.1 and {TRAVEL_MM} mm, not {mm}")
    level = min(range(len(LEVEL_TENTHS)), key=lambda i: abs(LEVEL_TENTHS[i] / 10 - mm))
    if level == 0:
        press, release = LEVEL_THRESHOLDS[1], LEVEL_THRESHOLDS[1]
    elif level == len(LEVEL_TENTHS) - 1:
        press, release = LEVEL_THRESHOLDS[-1], LEVEL_THRESHOLDS[-2]
    else:
        press, release = LEVEL_THRESHOLDS[2 * level + 2], LEVEL_THRESHOLDS[2 * level + 1]
    return LEVEL_TENTHS[level] / 10, press, release


def key_name(key):
    """Accept w, W, space, SPACE, comma, ','."""
    name = key.upper() if len(key) > 1 or key.isalpha() else key
    name = ALIASES.get(name, name)
    if name not in BY_NAME:
        raise ValueError(f"{key!r} has no sensor. Keys with one: " + " ".join(sorted(k for k in BY_NAME if not k.startswith("INT"))))
    return name


def key_names(keys):
    """None, 'WASD', 'W,A,SPACE', or a list."""
    if keys is None:
        return None
    if isinstance(keys, str):
        if "," in keys and keys != ",":
            keys = [k for k in keys.split(",") if k]
        elif keys.upper() in BY_NAME or keys.upper() in ALIASES or keys in BY_NAME:
            keys = [keys]
        else:
            keys = list(keys)
    return [key_name(k) for k in keys]


def _read(path):
    try:
        with open(path, errors="replace") as f:
            return f.read().strip()
    except OSError:
        return None


def _usb_product_present(product):
    return any(_read(f"{d}/idVendor") == VENDOR and _read(f"{d}/idProduct") == product for d in glob.glob("/sys/bus/usb/devices/*"))


def _find_control_node():
    """The keyboard's vendor interface, and the firmware of the USB device it belongs to."""
    for hidraw in sorted(glob.glob("/sys/class/hidraw/hidraw*")):
        uevent = _read(f"{hidraw}/device/uevent") or ""
        if f"0000{VENDOR}:0000{PRODUCT}".upper() not in uevent.upper():
            continue
        try:
            with open(f"{hidraw}/device/report_descriptor", "rb") as f:
                if not f.read().startswith(b"\x06\xc0\xff"):
                    continue
        except OSError:
            continue
        path = os.path.realpath(f"{hidraw}/device")
        while path != "/" and not os.path.exists(f"{path}/bcdDevice"):
            path = os.path.dirname(path)
        return "/dev/" + os.path.basename(hidraw), _read(f"{path}/bcdDevice")
    return None, None


class Keyboard:
    """The keyboard's sensors. Keys are named like 'W', 'SPACE', 'LSHIFT'; distances are millimetres, 0.0 to 4.0.

    with Keyboard() as kb:
        kb.keys                 names of every key with a sensor
        kb.read()               {'W': 1.24, 'A': 0.0, ...}, kb.read('WASD') for only those (and faster)
        kb.raw('W')             the filtered sensor values
        kb.stream(hz=100)       yields read() forever at that rate
        kb.events()             yields Event(key, kind, mm, velocity, time) for each press and release
        kb.actuation()          each key's press and release points in mm, and its mode byte
        kb.actuation_icon()     whether the OLED's ACTUATION ICON is on
    """

    def __init__(self):
        node, firmware = _find_control_node()
        if not node:
            if _usb_product_present(BOOTLOADER_PRODUCT):
                raise NotFound("The keyboard is in its emergency bootloader (flashing red). Repair it with SteelSeries GG on Windows or macOS.")
            raise NotFound(f"No Apex Pro Gen 3 ({VENDOR}:{PRODUCT}) found. Is it plugged in?")
        if firmware != FIRMWARE:
            raise ApexError(f"Refusing to run: this keyboard's firmware is {firmware}, and the commands here were only checked on "
                            f"{FIRMWARE} (4.16.8). On other firmware they could do something else.")
        self.node = node
        self.firmware = firmware
        try:
            self.fd = os.open(node, os.O_RDWR | os.O_NONBLOCK)
        except PermissionError:
            raise AccessDenied(f"No permission for {node}. Run ./install.sh from the apex-analog folder once.") from None
        try:
            fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(self.fd)
            self.fd = None
            raise ApexError("Another apex-analog is already reading the keyboard. Replies carry no ID, so only one can run at a time. "
                            "To feed several things, give one run several outputs.") from None
        self._calibration = {}
        try:
            for name, value in self._per_key(0xDA, 5, 5, list(BY_NAME)):
                top, rest = struct.unpack("<HH", value)
                if (top, rest) != NO_MAGNET and top > rest > 0:
                    self._calibration[name] = (top, rest)
        except Disconnected:
            self.close()
            raise
        self.keys = sorted(self._calibration, key=lambda n: BY_NAME[n][0])

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def close(self):
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None

    def _ask(self, payload, valid=None):
        if not payload or payload[0] not in ALLOWED:
            raise ValueError(f"refused to send {payload[:1]!r}: not a known read command")
        packet = bytes([0, *payload]).ljust(65, b"\0")[:65]
        try:
            for _ in range(3):
                while select.select([self.fd], [], [], 0)[0]:  # replies carry no ID, so never leave an old one waiting
                    os.read(self.fd, 64)
                os.write(self.fd, packet)
                deadline = time.monotonic() + 0.1
                while True:
                    left = deadline - time.monotonic()
                    if left <= 0 or not select.select([self.fd], [], [], left)[0]:
                        break
                    reply = os.read(self.fd, 64)
                    if valid is None or valid(reply):
                        return reply
        except OSError as e:
            raise Disconnected(f"Lost the keyboard: {e}") from None
        raise Disconnected("The keyboard stopped answering.")

    def _per_key(self, command, request_stride, reply_stride, names):
        """Ask a per-key read command about these keys, 12 at a time. Yields (name, the reply bytes after the key code)."""
        for start in range(0, len(names), 12):
            part = names[start:start + 12]
            hids = [BY_NAME[n][1] for n in part]
            request = [command, len(part)]
            for hid in hids:
                request += [hid] + [0] * (request_stride - 1)
            reply = self._ask(request, lambda r, h=tuple(hids): len(r) >= len(h) * reply_stride and all(r[i * reply_stride] == x for i, x in enumerate(h)))
            for i, name in enumerate(part):
                yield name, reply[i * reply_stride + 1:(i + 1) * reply_stride]

    def _bank(self, bank):
        reply = self._ask([0xD7, bank], _valid_bank)
        return struct.unpack("<14H", reply[28:56])

    def raw(self, keys=None):
        """Filtered sensor values, before calibration."""
        names = key_names(keys) or self.keys
        missing = [n for n in names if n not in self._calibration]
        if missing:
            raise ValueError(f"no sensor fitted on this keyboard for: {' '.join(missing)}")
        channels = {BY_NAME[n][0]: n for n in names}
        out = {}
        for bank in sorted({c // BANK_SIZE for c in channels}):
            values = self._bank(bank + 1)
            for i, value in enumerate(values):
                name = channels.get(bank * BANK_SIZE + i)
                if name:
                    out[name] = value
        return out

    def read(self, keys=None):
        """Depth of each key in mm. 0.0 when untouched, 4.0 at the bottom."""
        out = {}
        for name, value in self.raw(keys).items():
            top, rest = self._calibration[name]
            mm = curve_mm(255.0 * (value - rest) / (top - rest))
            out[name] = mm if mm >= AT_REST_MM else 0.0
        return out

    def stream(self, hz=100, keys=None):
        """Yields read() forever at up to hz. All keys tops out near 190 Hz; two rows of keys near 490 Hz."""
        names = key_names(keys)
        period, due = 1.0 / hz, time.monotonic()
        while True:
            yield self.read(names)
            due += period
            delay = due - time.monotonic()
            if delay > 0:
                time.sleep(delay)
            else:
                due = time.monotonic()

    def events(self, hz=200, keys=None, down=0.5, up=0.3):
        """Yields an Event each time a key goes past `down` mm, or back above `up` mm, with its speed in mm/s."""
        tracker = _Tracker(down, up)
        for depths in self.stream(hz, keys):
            yield from tracker.update(depths, time.monotonic())

    def actuation(self, keys=None):
        """{'W': {'press': 1.89, 'release': 1.8, 'press_raw': 50, 'release_raw': 46, 'mode': 0}} from the keyboard's settings."""
        names = key_names(keys) or self.keys
        out = {}
        for name, value in self._per_key(0xB1, 3, 3, names):
            out[name] = {"press": round(curve_mm(value[0]), 2), "release": round(curve_mm(value[1]), 2),
                         "press_raw": value[0], "release_raw": value[1]}
        for name, value in self._per_key(0xB2, 2, 2, names):
            out[name]["mode"] = value[0]
        return out

    def actuation_icon(self):
        """Whether the OLED's ACTUATION ICON setting is on. With DISPLAY ICONS also on, the firmware's home screen draws a depth bar."""
        return bool(self._ask([0x9E])[0])

    def set_actuation(self, keys, mm):
        """Set where these keys actuate, at the nearest firmware level. Returns the level used. LGUI can't be changed this way."""
        names = [n for n in key_names(keys) if n != "LGUI"]
        level, press, release = actuation_level(mm)
        for start in range(0, len(names), 20):
            part = names[start:start + 20]
            self._write([0x31, len(part)] + [byte for name in part for byte in (BY_NAME[name][1], press, release)])
        return level

    def _write(self, payload):
        """Send a command that gets no reply, then give the keyboard time to act on it. One that arrives straight after appears to replace it."""
        try:
            os.write(self.fd, bytes([0, *payload]).ljust(65, b"\0")[:65])
        except OSError as e:
            raise Disconnected(f"Lost the keyboard: {e}") from None
        time.sleep(WRITE_GAP)

    def _send_light(self, payload):
        if not payload or payload[0] not in LIGHT_COMMANDS:
            raise ValueError(f"refused to send {payload[:1]!r}: not a lighting command")
        self._write(payload)

    def _send_colors(self, items):
        for start in range(0, len(items), 15):
            part = items[start:start + 15]
            self._send_light([0x3A, len(part)] + [byte for item in part for byte in item])

    def set_colors(self, colors):
        """{'W': (255, 80, 0)}. Stays that colour until you change it, call clear_colors(), or replug."""
        self._send_colors([[BY_NAME[key_name(key)][1], *color] for key, color in colors.items()])

    def set_all_colors(self, red, green, blue):
        """Every key on the board, including the ones with no sensor."""
        self._send_colors([[hid, red, green, blue] for hid in LED_HIDS])

    def clear_colors(self):
        """Back to the keyboard's normal lighting."""
        self._send_light([0x3B])


def _valid_bank(reply):
    """D7 replies carry no ID. A stray report (menu use on the keyboard sends some) is mostly zeros, sensors never read 0."""
    return len(reply) >= 56 and all(0 < v < 4096 for v in struct.unpack("<28H", reply[:56]))


def stream(hz=100, keys=None):
    """for depths in apex_analog.stream(): ..."""
    with Keyboard() as kb:
        yield from kb.stream(hz, keys)


def events(hz=200, keys=None, down=0.5, up=0.3):
    """for event in apex_analog.events(): ..."""
    with Keyboard() as kb:
        yield from kb.events(hz, keys, down, up)


class Event(NamedTuple):
    key: str
    kind: str  # "down" or "up"
    mm: float
    velocity: float  # mm/s, positive going down
    time: float


class _Tracker:
    def __init__(self, down=0.5, up=0.3):
        self.down, self.up = down, up
        self.previous, self.held = {}, set()

    def update(self, depths, now):
        out = []
        for key, mm in depths.items():
            last_mm, last_time = self.previous.get(key, (mm, now))
            velocity = (mm - last_mm) / (now - last_time) if now > last_time else 0.0
            self.previous[key] = (mm, now)
            if key not in self.held and mm >= self.down:
                self.held.add(key)
                out.append(Event(key, "down", round(mm, 2), round(velocity, 1), now))
            elif key in self.held and mm <= self.up:
                self.held.discard(key)
                out.append(Event(key, "up", round(mm, 2), round(velocity, 1), now))
        return out


# ---- virtual devices -------------------------------------------------------------------------------------------------

EV_SYN, EV_KEY, EV_REL, EV_ABS = 0, 1, 2, 3
UI_SET_EVBIT, UI_SET_KEYBIT, UI_SET_RELBIT, UI_SET_ABSBIT = 0x40045564, 0x40045565, 0x40045566, 0x40045567
UI_DEV_SETUP, UI_ABS_SETUP, UI_DEV_CREATE, UI_DEV_DESTROY = 0x405C5503, 0x401C5504, 0x5501, 0x5502
EVIOCGRAB = 0x40044590
BUS_USB, BUS_VIRTUAL = 3, 6
KEY_BYTES = 96  # room for every key code up to KEY_MAX


def _ior(base, number, size):
    return (2 << 30) | (size << 16) | (ord(base) << 8) | number


class _Uinput:
    def __init__(self, name, ids, keys=(), rels=(), abses=None):
        try:
            self.fd = os.open("/dev/uinput", os.O_WRONLY | os.O_NONBLOCK)
        except PermissionError:
            raise AccessDenied("No permission for /dev/uinput. Run ./install.sh from the apex-analog folder once.") from None
        except FileNotFoundError:
            raise AccessDenied("/dev/uinput is missing. Load the module: sudo modprobe uinput") from None
        if keys:
            fcntl.ioctl(self.fd, UI_SET_EVBIT, EV_KEY)
            for code in keys:
                fcntl.ioctl(self.fd, UI_SET_KEYBIT, code)
        if rels:
            fcntl.ioctl(self.fd, UI_SET_EVBIT, EV_REL)
            for code in rels:
                fcntl.ioctl(self.fd, UI_SET_RELBIT, code)
        if abses:
            fcntl.ioctl(self.fd, UI_SET_EVBIT, EV_ABS)
            for code in abses:
                fcntl.ioctl(self.fd, UI_SET_ABSBIT, code)
        fcntl.ioctl(self.fd, UI_DEV_SETUP, struct.pack("<4H80sI", *ids, name.encode()[:79], 0))
        for code, (low, high) in (abses or {}).items():
            fcntl.ioctl(self.fd, UI_ABS_SETUP, struct.pack("<H2x6i", code, 0, low, high, 0, 0, 0))
        fcntl.ioctl(self.fd, UI_DEV_CREATE)

    @property
    def path(self):
        """This device's /dev/input/eventN."""
        buffer = bytearray(64)
        fcntl.ioctl(self.fd, _ior("U", 44, 64), buffer)
        sysname = buffer.split(b"\0")[0].decode()
        found = glob.glob(f"/sys/devices/virtual/input/{sysname}/event*")
        return "/dev/input/" + os.path.basename(found[0]) if found else None

    def emit(self, kind, code, value):
        os.write(self.fd, struct.pack("llHHi", 0, 0, kind, code, value))

    def sync(self):
        self.emit(EV_SYN, 0, 0)

    def close(self):
        if self.fd is not None:
            fcntl.ioctl(self.fd, UI_DEV_DESTROY)
            os.close(self.fd)
            self.fd = None


class Gamepad:
    """A virtual controller with the Xbox 360 layout, which games and Proton already understand.

    with Gamepad() as pad:
        pad.set(lx=-0.5, rt=0.8, a=True, dpad_up=False)

    Sticks lx ly rx ry run -1 to 1, triggers lt rt run 0 to 1, the rest are True or False.
    """
    STICKS = {"lx": 0x00, "ly": 0x01, "rx": 0x03, "ry": 0x04}
    TRIGGERS = {"lt": 0x02, "rt": 0x05}
    BUTTONS = {"a": 0x130, "b": 0x131, "x": 0x133, "y": 0x134, "lb": 0x136, "rb": 0x137, "back": 0x13A, "start": 0x13B,
               "guide": 0x13C, "ls": 0x13D, "rs": 0x13E}
    DPAD = {"dpad_left": (0x10, -1), "dpad_right": (0x10, 1), "dpad_up": (0x11, -1), "dpad_down": (0x11, 1)}
    STICK_MAX, TRIGGER_MAX = 32767, 1023

    def __init__(self, name="Apex Analog (Xbox 360 layout)"):
        abses = {code: (-32768, self.STICK_MAX) for code in self.STICKS.values()}
        abses.update({code: (0, self.TRIGGER_MAX) for code in self.TRIGGERS.values()})
        abses.update({0x10: (-1, 1), 0x11: (-1, 1)})
        self.device = _Uinput(name, (BUS_USB, 0x045E, 0x028E, 0x0110), keys=self.BUTTONS.values(), abses=abses)
        self.path = self.device.path
        self.values, self._sent = {}, {}

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def _send(self, kind, code, value):
        if self._sent.get((kind, code), 0) != value:
            self._sent[(kind, code)] = value
            self.device.emit(kind, code, value)
            return True
        return False

    def set(self, **values):
        changed = False
        for name, value in values.items():
            self.values[name] = value
            if name in self.STICKS:
                changed |= self._send(EV_ABS, self.STICKS[name], int(max(-1.0, min(1.0, value)) * self.STICK_MAX))
            elif name in self.TRIGGERS:
                changed |= self._send(EV_ABS, self.TRIGGERS[name], int(max(0.0, min(1.0, value)) * self.TRIGGER_MAX))
            elif name in self.BUTTONS:
                changed |= self._send(EV_KEY, self.BUTTONS[name], int(bool(value)))
            elif name in self.DPAD:
                axis = self.DPAD[name][0]
                hat = sum(sign * bool(self.values.get(n)) for n, (a, sign) in self.DPAD.items() if a == axis)
                changed |= self._send(EV_ABS, axis, hat)
            else:
                raise ValueError(f"no control called {name!r}")
        if changed:
            self.device.sync()

    def center(self):
        self.set(**{n: 0.0 for n in {**self.STICKS, **self.TRIGGERS}}, **{n: False for n in {**self.BUTTONS, **self.DPAD}})

    def close(self):
        self.center()
        self.device.close()


class Mouse:
    """A virtual mouse: mouse.move(dx, dy), mouse.scroll(1), mouse.button('left', True)."""
    BUTTONS = {"left": 0x110, "right": 0x111, "middle": 0x112}
    AXES = {"x": 0x00, "y": 0x01, "hwheel": 0x06, "wheel": 0x08}

    def __init__(self, name="Apex Analog Mouse"):
        self.device = _Uinput(name, (BUS_VIRTUAL, 0x1209, 0x0001, 1), keys=self.BUTTONS.values(), rels=self.AXES.values())
        self.path = self.device.path

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def move(self, dx=0, dy=0):
        self._rel(x=dx, y=dy)

    def scroll(self, amount=0, horizontal=0):
        self._rel(wheel=amount, hwheel=horizontal)

    def _rel(self, **amounts):
        moved = False
        for axis, amount in amounts.items():
            if amount:
                self.device.emit(EV_REL, self.AXES[axis], int(amount))
                moved = True
        if moved:
            self.device.sync()

    def button(self, name, down):
        self.device.emit(EV_KEY, self.BUTTONS[name], int(bool(down)))
        self.device.sync()

    def close(self):
        for code in self.BUTTONS.values():
            self.device.emit(EV_KEY, code, 0)
        self.device.sync()
        self.device.close()


class Pedals:
    """Extra analog levers on a device of their own, so they reach a game with no default bindings to collide with.

    with Pedals() as pedals:
        pedals.set(clutch=0.5, handbrake=1.0)
    """
    AXES = {"clutch": 0x00, "handbrake": 0x01, "pedal3": 0x02, "pedal4": 0x03, "pedal5": 0x04, "pedal6": 0x05}
    MAX = 4095

    def __init__(self, name="Apex Analog Pedals"):
        self.device = _Uinput(name, (BUS_USB, 0x1209, 0x0003, 1), keys=[0x120], abses={code: (0, self.MAX) for code in self.AXES.values()})
        self.path = self.device.path
        self._sent = {}

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def set(self, **values):
        changed = False
        for name, value in values.items():
            number = int(max(0.0, min(1.0, value)) * self.MAX)
            if self._sent.get(name, 0) != number:
                self._sent[name] = number
                self.device.emit(EV_ABS, self.AXES[name], number)
                changed = True
        if changed:
            self.device.sync()

    def close(self):
        self.set(**{name: 0.0 for name in self.AXES})
        self.device.close()


# Names for keystroke outputs: every key with a sensor, plus the ones without.
KEYCODES = {name: code for name, (_, _, code) in BY_NAME.items() if code is not None}
KEYCODES.update({"ESC": 1, "PRTSC": 99, "SCRLK": 70, "PAUSE": 119, "INSERT": 110, "DELETE": 111, "HOME": 102, "END": 107, "PGUP": 104,
                 "PGDN": 109, "UP": 103, "LEFT": 105, "RIGHT": 106, "DOWN": 108, "MENU": 127,
                 **{f"F{i}": 58 + i for i in range(1, 11)}, "F11": 87, "F12": 88, **{f"F{i}": 170 + i for i in range(13, 25)}})


def keycode(name):
    upper = name.upper() if len(name) > 1 or name.isalpha() else name
    upper = ALIASES.get(upper, upper)
    if upper not in KEYCODES:
        raise ValueError(f"unknown key {name!r}. Known: " + " ".join(sorted(KEYCODES)))
    return KEYCODES[upper]


class VirtualKeys:
    """A virtual keyboard: keys.press('F13'), keys.release('F13'), keys.tap('F13')."""

    def __init__(self, codes=None, name="Apex Analog Keys"):
        self.device = _Uinput(name, (BUS_VIRTUAL, 0x1209, 0x0002, 1), keys=sorted(set(codes or KEYCODES.values())))
        self.path = self.device.path
        self.down = set()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def send(self, code, value):
        """value 1 down, 0 up, 2 auto-repeat."""
        if value:
            self.down.add(code)
        else:
            self.down.discard(code)
        self.device.emit(EV_KEY, code, value)
        self.device.sync()

    def press(self, name):
        self.send(keycode(name), 1)

    def release(self, name):
        self.send(keycode(name), 0)

    def tap(self, name):
        self.press(name)
        self.release(name)

    def close(self):
        for code in list(self.down):
            self.send(code, 0)
        self.device.close()


def _physical_key_nodes():
    """The keyboard's own input devices that type letters."""
    nodes = []
    for event in sorted(glob.glob("/sys/class/input/event*")):
        if _read(f"{event}/device/id/vendor") == VENDOR and _read(f"{event}/device/id/product") == PRODUCT:
            nodes.append("/dev/input/" + os.path.basename(event))
    return nodes


def _key_bits(fd):
    buffer = bytearray(KEY_BYTES)
    fcntl.ioctl(fd, _ior("E", 0x20 + EV_KEY, KEY_BYTES), buffer)
    return {i * 8 + bit for i, byte in enumerate(buffer) for bit in range(8) if byte >> bit & 1}


def _any_key_held(fd):
    buffer = bytearray(KEY_BYTES)
    fcntl.ioctl(fd, _ior("E", 0x18, KEY_BYTES), buffer)
    return any(buffer)


class Swallow(threading.Thread):
    """Stops chosen keys from typing while everything else types as normal.

    Takes exclusive hold of the keyboard's input devices and passes every other key through a virtual keyboard. The kernel
    drops the hold when this process ends for any reason, so a crash cannot leave the keyboard dead.
    """

    def __init__(self, blocked, output, pause_code=None, nodes=None):
        super().__init__(daemon=True)
        self.blocked, self.output, self.pause_code = set(blocked), output, pause_code
        self.paused = False
        self._stop_flag = False
        self.fds = []
        for node in (_physical_key_nodes() if nodes is None else nodes):
            fd = os.open(node, os.O_RDONLY | os.O_NONBLOCK)
            if BY_NAME["W"][2] in _key_bits(fd) or nodes is not None:
                self.fds.append(fd)
            else:
                os.close(fd)

    def capabilities(self):
        codes = set()
        for fd in self.fds:
            codes |= _key_bits(fd)
        return codes

    def grab(self, wait=3.0):
        """Waits for held keys to come up first: a key held across the grab would look stuck down to the desktop."""
        deadline = time.monotonic() + wait
        while any(_any_key_held(fd) for fd in self.fds):
            if time.monotonic() > deadline:
                return False
            time.sleep(0.02)
        for fd in self.fds:
            fcntl.ioctl(fd, EVIOCGRAB, 1)
        self.start()
        return True

    def run(self):
        size = struct.calcsize("llHHi")
        try:
            while not self._stop_flag:
                for fd in select.select(self.fds, [], [], 0.2)[0]:
                    data = os.read(fd, size * 64)
                    for offset in range(0, len(data) - size + 1, size):
                        _, _, kind, code, value = struct.unpack_from("llHHi", data, offset)
                        if kind == EV_KEY:
                            self._key(code, value)
        except OSError:
            pass  # unplugged; the main loop notices too
        finally:
            for fd in self.fds:
                try:
                    os.close(fd)  # closing also drops the grab
                except OSError:
                    pass
            self.fds = []

    def _key(self, code, value):
        if code == self.pause_code:
            if value == 1:
                self.paused = not self.paused
                if not self.paused:
                    for held in self.output.down & self.blocked:  # a key typed during the pause must not stay down
                        self.output.send(held, 0)
            return
        if code in self.blocked and not self.paused:
            return
        self.output.send(code, value)

    def stop(self):
        self._stop_flag = True
        if self.is_alive():
            self.join(1.0)


# ---- mapping keys to outputs -----------------------------------------------------------------------------------------

PRESETS = {
    "drive": "W=rt S=lt A=lx- D=lx+",
    "stick": "W=ly- S=ly+ A=lx- D=lx+",
    "mouse": "I=mouse-y- K=mouse-y+ J=mouse-x- L=mouse-x+ U=wheel+ O=wheel- H=mouse-left ;=mouse-right",
    "drive2": "W=rt S=lt A=lx- D=lx+ I=p2:rt K=p2:lt J=p2:lx- L=p2:lx+",
    "stick2": "W=ly- S=ly+ A=lx- D=lx+ I=p2:ly- K=p2:ly+ J=p2:lx- L=p2:lx+",
    "manual": "W=rt S=lt A=lx- D=lx+ LSHIFT=clutch SPACE=handbrake",
}
RUN_TARGETS = {
    "volume+": "wpctl set-volume -l 1 @DEFAULT_AUDIO_SINK@ 2%+",
    "volume-": "wpctl set-volume @DEFAULT_AUDIO_SINK@ 2%-",
    "brightness+": "brightnessctl -q set +2%",
    "brightness-": "brightnessctl -q set 2%-",
}
HYSTERESIS_MM = 0.15


class Digital:
    """Turns depth into on/off at a set point. With rapid > 0 it also re-triggers on direction changes anywhere in the travel."""

    def __init__(self, at=1.0, rapid=0.0):
        self.at, self.rapid = at, rapid
        self.down = False
        self.peak = self.trough = 0.0

    def update(self, mm):
        if self.down:
            self.peak = max(self.peak, mm)
            if mm < self.at - HYSTERESIS_MM or (self.rapid and mm <= self.peak - self.rapid):
                self.down, self.trough = False, mm
        else:
            self.trough = min(self.trough, mm)
            if mm >= self.at and (not self.rapid or mm >= self.trough + self.rapid):
                self.down, self.peak = True, mm
        return self.down


class Mapping:
    """One 'KEY=OUTPUT' or 'KEY=OUTPUT@mm' rule."""

    def __init__(self, text, rapid=0.0):
        source, separator, target = text[1:].partition("=")  # text[0] may itself be the = key
        if not separator or not target:
            raise ValueError(f"{text!r} should look like W=rt or J=key:F13@2.0")
        source = text[0] + source
        self.text, self.key = text, key_name(source)
        at = 1.0
        self.player = 1
        self.sign = 1
        self.digital = None
        self.owed, self.active, self.process = 0.0, False, None
        if target.startswith("p2:"):
            self.player, target = 2, target[3:]
        if "@" in target and not target.startswith("run:"):
            target, at_text = target.rsplit("@", 1)
            at = float(at_text)
        if target.startswith("key:"):
            self.kind, self.target = "key", keycode(target[4:])
        elif target.startswith("tap:"):
            self.kind, self.target = "tap", keycode(target[4:])
        elif target.startswith("run:") and target[4:]:
            self.kind, self.target = "run", target[4:]
        elif target in RUN_TARGETS:
            self.kind, self.target = "run", RUN_TARGETS[target]
        elif target in Pedals.AXES:
            self.kind, self.target = "pedal", target
        elif target.startswith("mouse-") and target[6:] in Mouse.BUTTONS:
            self.kind, self.target = "mouse-button", target[6:]
        elif target.replace("-", "_") in Gamepad.BUTTONS or target.replace("-", "_") in Gamepad.DPAD:
            self.kind, self.target = "button", target.replace("-", "_")
        elif target in Gamepad.TRIGGERS:
            self.kind, self.target = "trigger", target
        elif target[-1:] in "+-" and target[:-1] in Gamepad.STICKS:
            self.kind, self.target, self.sign = "stick", target[:-1], (1 if target[-1] == "+" else -1)
        elif target[-1:] in "+-" and target[:-1] in ("mouse-x", "mouse-y", "wheel", "hwheel"):
            self.kind, self.target, self.sign = "mouse-axis", target[:-1].replace("mouse-", ""), (1 if target[-1] == "+" else -1)
        else:
            raise ValueError(f"unknown output {target!r} in {text!r}. Sticks: lx- lx+ ly- ly+ rx- rx+ ry- ry+. Triggers: lt rt. "
                             f"Buttons: {' '.join(Gamepad.BUTTONS)} dpad-up dpad-down dpad-left dpad-right. "
                             "Add p2: for a second controller. Pedals: " + " ".join(Pedals.AXES) + ". "
                             "Mouse: mouse-x- mouse-x+ mouse-y- mouse-y+ wheel- wheel+ hwheel- hwheel+ mouse-left mouse-right mouse-middle. "
                             "Keystroke: key:F13. Repeating keystroke: tap:RIGHT. Desktop: volume+ volume- brightness+ brightness-. "
                             "Command: run:your-command")
        if self.player == 2 and self.kind not in ("stick", "trigger", "button"):
            raise ValueError(f"{text!r}: p2: only applies to controller outputs")
        if self.kind in ("key", "mouse-button", "button"):
            self.digital = Digital(at, rapid)


def expand_maps(items):
    out = []
    for item in items:
        out += PRESETS[item].split() if item in PRESETS else [item]
    return out


class _KeyHolder:
    """What every key-driven mode shares: holding its keys back from typing, the virtual keyboard, and the pause key."""

    def __init__(self, swallow=True, pause_key="PAUSE"):
        self.want_swallow, self.pause_code = swallow, keycode(pause_key) if pause_key else None
        self.keys, self.key_outputs = [], set()
        self.virtual_keys, self.swallow = None, None
        self.warned = False

    @property
    def paused(self):
        return bool(self.swallow and self.swallow.paused)

    def held_keys(self):
        return self.keys

    def attach(self):
        """Hold back these keys and pass the rest through. Without access this still works; the keys also keep typing."""
        blocked = {BY_NAME[k][2] for k in self.held_keys() if BY_NAME[k][2] is not None}
        swallow = None
        if self.want_swallow:
            try:
                swallow = Swallow(blocked, None, self.pause_code)
            except OSError as e:
                self._warn(f"These keys will keep typing: no access to the keyboard's input devices ({e}). Run ./install.sh once.")
        codes = set(self.key_outputs)
        if swallow:
            codes |= swallow.capabilities()
        if codes and not self.virtual_keys:
            self.virtual_keys = VirtualKeys(codes)
            time.sleep(0.2)  # let the desktop notice the new keyboard before anything is forwarded to it
        if swallow:
            swallow.output = self.virtual_keys
            if swallow.grab():
                self.swallow = swallow
            else:
                for fd in swallow.fds:
                    os.close(fd)
                self._warn("These keys will keep typing: a key stayed held down, so the keyboard was not taken over.")

    def _warn(self, message):
        if not self.warned:
            _say(message)
            self.warned = True

    def stop_holding(self):
        if self.swallow:
            self.swallow.stop()
            self.swallow = None


class Mapper(_KeyHolder):
    """Turns key depths into controller, pedal, mouse, keystroke and command output. Nothing is created until it is needed."""

    def __init__(self, maps, deadzone=0.3, full=3.5, curve=1.0, rapid=0.0, mouse_speed=1200.0, wheel_speed=15.0,
                 tap_rate=20.0, run_rate=8.0, swallow=True, pause_key="PAUSE"):
        super().__init__(swallow, pause_key)
        if not full > deadzone:
            raise ValueError("--full must be deeper than --deadzone")
        self.maps = [Mapping(m, rapid) for m in expand_maps(maps)]
        if not self.maps:
            raise ValueError("nothing to map. Try: apex-analog map drive")
        self.deadzone, self.full, self.curve = deadzone, full, curve
        self.mouse_speed, self.wheel_speed, self.tap_rate, self.run_rate = mouse_speed, wheel_speed, tap_rate, run_rate
        self.keys = sorted({m.key for m in self.maps}, key=lambda n: BY_NAME[n][0])
        self.pads = {}
        for player in sorted({m.player for m in self.maps if m.kind in ("stick", "trigger", "button")}):
            self.pads[player] = Gamepad(f"Apex Analog Player {player} (Xbox 360 layout)")
        kinds = {m.kind for m in self.maps}
        self.pedals = Pedals() if "pedal" in kinds else None
        self.mouse = Mouse() if kinds & {"mouse-axis", "mouse-button"} else None
        self.key_outputs = {m.target for m in self.maps if m.kind in ("key", "tap")}
        self.remainder = {}
        self.values = {}

    @property
    def pad(self):
        return self.pads.get(1)

    def shape(self, mm):
        x = (mm - self.deadzone) / (self.full - self.deadzone)
        return max(0.0, min(1.0, x)) ** self.curve

    def _owed(self, m, rate, dt):
        """How many repeats this key has earned. The first comes as soon as the press starts counting."""
        if rate <= 0:
            m.owed, m.active = 0.0, False
            return 0
        m.owed += rate * dt + (1.0 if not m.active else 0.0)
        m.active = True
        whole = int(m.owed)
        m.owed -= whole
        return whole

    def update(self, depths, dt):
        if self.paused:
            depths = {}
        pad_values, pedal_values, keys, moves, clicks = {}, {}, {}, {}, {}
        for m in self.maps:
            mm = depths.get(m.key, 0.0)
            amount = self.shape(mm)
            slots = pad_values.setdefault(m.player, {})
            if m.kind == "stick":
                slots[m.target] = slots.get(m.target, 0.0) + m.sign * amount
            elif m.kind == "trigger":
                slots[m.target] = max(slots.get(m.target, 0.0), amount)
            elif m.kind == "button":
                slots[m.target] = m.digital.update(mm) or slots.get(m.target, False)
            elif m.kind == "pedal":
                pedal_values[m.target] = max(pedal_values.get(m.target, 0.0), amount)
            elif m.kind == "mouse-axis":
                rate = self.wheel_speed if "wheel" in m.target else self.mouse_speed
                moves[m.target] = moves.get(m.target, 0.0) + m.sign * amount * rate * dt
            elif m.kind == "mouse-button":
                clicks[m.target] = m.digital.update(mm) or clicks.get(m.target, False)
            elif m.kind == "key":
                keys[m.target] = m.digital.update(mm) or keys.get(m.target, False)
            elif m.kind == "tap":
                for _ in range(self._owed(m, amount * self.tap_rate, dt)):
                    if self.virtual_keys:
                        self.virtual_keys.send(m.target, 1)
                        self.virtual_keys.send(m.target, 0)
            elif m.kind == "run":
                if self._owed(m, amount * self.run_rate, dt) and (m.process is None or m.process.poll() is not None):
                    m.process = subprocess.Popen(m.target, shell=True, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        values = {}
        for player, pad in self.pads.items():
            pad.set(**pad_values.get(player, {}))
            prefix = "" if player == 1 else f"p{player}:"
            values.update({prefix + k: v for k, v in pad_values.get(player, {}).items()})
        if self.pedals:
            self.pedals.set(**pedal_values)
            values.update(pedal_values)
        self.values = values
        if self.mouse:
            whole = {}
            for axis, amount in moves.items():
                total = self.remainder.get(axis, 0.0) + amount
                whole[axis] = int(total)
                self.remainder[axis] = total - whole[axis]
            self.mouse._rel(**whole)
            for name, state in clicks.items():
                if self.remainder.get(("button", name)) != state:
                    self.remainder[("button", name)] = state
                    self.mouse.button(name, state)
        if self.virtual_keys:
            for code, state in keys.items():
                if state != (code in self.virtual_keys.down):
                    self.virtual_keys.send(code, int(state))

    def release(self):
        """Let go of everything: nothing may stay pressed once the keyboard is gone."""
        for m in self.maps:
            if m.digital:
                m.digital.down = False
        self.update({}, 0.0)
        self.stop_holding()

    def close(self):
        self.release()
        for device in (*self.pads.values(), self.pedals, self.mouse, self.virtual_keys):
            if device:
                device.close()


# ---- MIDI ------------------------------------------------------------------------------------------------------------

NOTE_NAMES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]
MIDI_NOTE_ON, MIDI_NOTE_OFF, MIDI_KEY_PRESSURE, MIDI_CONTROLLER, MIDI_CHANNEL_PRESSURE, MIDI_PITCH_BEND = 6, 7, 8, 10, 12, 13


def note_name(note):
    return f"{NOTE_NAMES[note % 12]}{note // 12 - 1}"


def midi_layout(name="piano", octave=0):
    """Key name -> MIDI note. Z is C3 in the piano layout; each --octave moves everything 12 notes."""
    base = 48 + 12 * octave
    if name == "piano":  # the shape of a piano: the row above each white-key row holds the black keys
        rows = [("ZSXDCVGBHNJM,L.;/", base), ("Q2W3ER5T6Y7UI9O0P[=]", base + 12)]
    elif name == "grid":  # every key one note up from its left neighbour, every row a fourth above the one below, like a guitar
        rows = [("ZXCVBNM,./", base - 12), ("ASDFGHJKL;'", base - 7), ("QWERTYUIOP[]\\", base - 2), ("1234567890-=", base + 3)]
    else:
        raise ValueError(f"unknown layout {name!r}. Layouts: piano grid")
    return {key_name(key): start + i for keys, start in rows for i, key in enumerate(keys) if 0 <= start + i <= 127}


def midi_velocity(speed, soft=8.0, hard=160.0):
    """Press speed in mm/s -> MIDI velocity 1-127, on a log scale, because that's how loudness feels."""
    if speed <= soft:
        return 1
    return min(127, 1 + int(126 * math.log(speed / soft) / math.log(hard / soft)))


class AlsaMidi:
    """A virtual MIDI port any ALSA, JACK or PipeWire app can connect to. Uses libasound directly, so no Python packages."""

    def __init__(self, name="Apex Analog"):
        library = ctypes.util.find_library("asound")
        if not library:
            raise ApexError("MIDI needs libasound (alsa-lib), which wasn't found.")
        self.lib = ctypes.CDLL(library)
        void = ctypes.c_void_p
        self.lib.snd_seq_open.argtypes = [ctypes.POINTER(void), ctypes.c_char_p, ctypes.c_int, ctypes.c_int]
        self.lib.snd_seq_set_client_name.argtypes = [void, ctypes.c_char_p]
        self.lib.snd_seq_create_simple_port.argtypes = [void, ctypes.c_char_p, ctypes.c_uint, ctypes.c_uint]
        self.lib.snd_seq_client_id.argtypes = [void]
        self.lib.snd_seq_event_output_direct.argtypes = [void, ctypes.c_char_p]
        self.lib.snd_seq_close.argtypes = [void]
        self.seq = void()
        if self.lib.snd_seq_open(ctypes.byref(self.seq), b"default", 1, 1) < 0:  # output only, never block the key loop
            raise AccessDenied("Could not open the ALSA sequencer (/dev/snd/seq). Is your user in the audio group?")
        self.lib.snd_seq_set_client_name(self.seq, name.encode())
        read_and_subscribe, midi_application = (1 << 0) | (1 << 5), (1 << 1) | (1 << 20)
        self.port = self.lib.snd_seq_create_simple_port(self.seq, name.encode(), read_and_subscribe, midi_application)
        self.client = self.lib.snd_seq_client_id(self.seq)
        self.address = f"{self.client}:{self.port}"

    def _send(self, kind, data):
        direct_queue, to_subscribers, unknown_port = 253, 254, 253
        event = struct.pack("<BBBB8xBBBB12s", kind, 0, 0, direct_queue, 0, self.port, to_subscribers, unknown_port, data)
        self.lib.snd_seq_event_output_direct(self.seq, event)

    def note_on(self, channel, note, velocity):
        self._send(MIDI_NOTE_ON, struct.pack("<BBBBI", channel, note, velocity, 0, 0))

    def note_off(self, channel, note, velocity=64):
        self._send(MIDI_NOTE_OFF, struct.pack("<BBBBI", channel, note, velocity, 0, 0))

    def key_pressure(self, channel, note, pressure):
        self._send(MIDI_KEY_PRESSURE, struct.pack("<BBBBI", channel, note, pressure, 0, 0))

    def controller(self, channel, number, value):
        self._send(MIDI_CONTROLLER, struct.pack("<B3xIi", channel, number, value))

    def channel_pressure(self, channel, pressure):
        self._send(MIDI_CHANNEL_PRESSURE, struct.pack("<B3xIi", channel, 0, pressure))

    def pitch_bend(self, channel, value):
        self._send(MIDI_PITCH_BEND, struct.pack("<B3xIi", channel, 0, value))

    def close(self):
        if self.seq:
            self.lib.snd_seq_close(self.seq)
            self.seq = None


class MidiInstrument(_KeyHolder):
    """Plays notes from key depth: press speed sets velocity, hold depth sets per-key pressure, SPACE sustains, LSHIFT is the
    mod wheel, TAB and CAPS bend pitch up and down."""

    def __init__(self, layout="piano", octave=0, channel=1, note_at=1.5, aftertouch="poly", soft=8.0, hard=160.0, ccs=None, output=None,
                 swallow=True, pause_key="PAUSE"):
        super().__init__(swallow, pause_key)
        if aftertouch not in ("poly", "channel", "off"):
            raise ValueError("--aftertouch should be poly, channel or off")
        if not 1 <= channel <= 16:
            raise ValueError("--channel should be 1 to 16")
        self.notes = midi_layout(layout, octave)
        self.channel, self.note_at, self.aftertouch, self.soft, self.hard = channel - 1, note_at, aftertouch, soft, hard
        self.ccs = {"SPACE": 64, "LSHIFT": 1}
        for key, number in (ccs or {}).items():
            self.ccs[key_name(key)] = number
        self.notes = {k: n for k, n in self.notes.items() if k not in self.ccs}
        self.bend_down, self.bend_up = "CAPS", "TAB"
        self.keys = sorted({*self.notes, *self.ccs, self.bend_down, self.bend_up}, key=lambda n: BY_NAME[n][0])
        self.midi = output or AlsaMidi()
        self.state = {key: {"on": False, "started": None, "pressure": 0} for key in self.notes}
        self.sounding = {}
        self.cc_values, self.bend = {}, 0
        self.now = 0.0

    def _press(self, note, velocity):
        self.sounding[note] = self.sounding.get(note, 0) + 1
        if self.sounding[note] == 1:  # two keys can share a note; it only starts once
            self.midi.note_on(self.channel, note, velocity)

    def _lift(self, note):
        self.sounding[note] -= 1
        if not self.sounding[note]:
            del self.sounding[note]
            self.midi.note_off(self.channel, note)

    def update(self, depths, dt):
        self.now += dt
        if self.paused:
            depths = {}
        strongest = 0
        for key, note in self.notes.items():
            mm, state = depths.get(key, 0.0), self.state[key]
            if not state["on"]:
                if mm < AT_REST_MM:
                    state["started"] = None
                elif state["started"] is None:
                    state["started"] = self.now - dt  # it left rest some time since the last reading
                if mm >= self.note_at:
                    speed = (self.note_at - AT_REST_MM) / max(self.now - state["started"], 0.001)
                    state["on"], state["pressure"] = True, 0
                    self._press(note, midi_velocity(speed, self.soft, self.hard))
            elif mm <= self.note_at - 0.4:
                state["on"], state["started"] = False, None
                self._lift(note)
            else:
                pressure = int(127 * max(0.0, min(1.0, (mm - self.note_at) / (TRAVEL_MM - self.note_at))))
                strongest = max(strongest, pressure)
                if self.aftertouch == "poly" and (abs(pressure - state["pressure"]) >= 2 or (pressure in (0, 127) and pressure != state["pressure"])):
                    state["pressure"] = pressure
                    self.midi.key_pressure(self.channel, note, pressure)
        if self.aftertouch == "channel" and strongest != self.cc_values.get("pressure", 0):
            self.cc_values["pressure"] = strongest
            self.midi.channel_pressure(self.channel, strongest)
        for key, number in self.ccs.items():
            value = int(127 * depths.get(key, 0.0) / TRAVEL_MM)
            last = self.cc_values.get(key, 0)
            if abs(value - last) >= 2 or (value in (0, 127) and value != last):
                self.cc_values[key] = value
                self.midi.controller(self.channel, number, value)
        bend = int(8191 * (depths.get(self.bend_up, 0.0) - depths.get(self.bend_down, 0.0)) / TRAVEL_MM)
        if abs(bend - self.bend) >= 64 or (bend == 0 and self.bend):
            self.bend = bend
            self.midi.pitch_bend(self.channel, bend)

    def release(self):
        self.update({}, 0.0)
        self.midi.controller(self.channel, 123, 0)  # all notes off, in case a synth missed one
        self.stop_holding()

    def close(self):
        self.release()
        self.midi.close()
        if self.virtual_keys:
            self.virtual_keys.close()


# ---- pressure typing -------------------------------------------------------------------------------------------------

MODIFIERS = ("LSHIFT", "RSHIFT", "LCTRL", "RCTRL", "LALT", "RALT", "LGUI", "RGUI")
TYPED_KEYS = "ABCDEFGHIJKLMNOPQRSTUVWXYZ1234567890"


class PressureTyper(_KeyHolder):
    """Type lightly for lowercase and press deep for a capital, or for a key combo you choose per key.

    A press only sends its keystroke once its depth is settled: it went deep, or it started rising, or another key went down,
    or it has been held for `hold` seconds. Keystrokes always go out in the order they were pressed.
    """

    def __init__(self, at=1.5, deep=3.2, hold=0.15, combos=None, pause_key="PAUSE"):
        super().__init__(True, pause_key)
        if not deep > at:
            raise ValueError("--deep must be deeper than --at")
        self.at, self.deep, self.hold = at, deep, hold
        self.combos = {key_name(k): [keycode(n) for n in v.split("+")] for k, v in (combos or {}).items()}
        self.typed = [key_name(k) for k in TYPED_KEYS]
        self.keys = sorted({*self.typed, *self.combos, *MODIFIERS}, key=lambda n: BY_NAME[n][0])
        self.typed = [k for k in self.keys if k not in MODIFIERS]
        self.key_outputs = {BY_NAME[k][2] for k in self.typed} | {BY_NAME["LSHIFT"][2]} | {c for combo in self.combos.values() for c in combo}
        self.presses, self.queue = {}, []
        self.now = 0.0

    def held_keys(self):
        return self.typed

    def update(self, depths, dt):
        self.now += dt
        if self.paused:
            depths = {}
        modifier_held = any(depths.get(k, 0.0) >= self.at for k in MODIFIERS)
        for key in self.typed:
            mm, press = depths.get(key, 0.0), self.presses.get(key)
            if press is None:
                if mm >= self.at:
                    for earlier in self.queue:  # another key went down, so earlier presses have settled
                        earlier["fate"] = earlier["fate"] or "light"
                    press = {"key": key, "peak": mm, "since": self.now, "fate": "light" if modifier_held else None, "sent": False, "released": False}
                    self.presses[key] = press
                    self.queue.append(press)
                continue
            press["peak"] = max(press["peak"], mm)
            if press["fate"] is None:
                if mm >= self.deep:
                    press["fate"] = "deep"
                elif mm <= press["peak"] - 0.3 or self.now - press["since"] >= self.hold:
                    press["fate"] = "light"
            if mm < self.at - HYSTERESIS_MM:
                press["fate"] = press["fate"] or "light"
                press["released"] = True
        self._flush()

    def _flush(self):
        while self.queue and self.queue[0]["fate"]:
            press = self.queue.pop(0)
            if self.virtual_keys:
                self._send_down(press)
            press["sent"] = True
        for key, press in list(self.presses.items()):
            if press["released"] and press["sent"]:
                if self.virtual_keys and press.get("code") is not None:
                    self.virtual_keys.send(press["code"], 0)
                del self.presses[key]

    def _send_down(self, press):
        code = BY_NAME[press["key"]][2]
        output = self.virtual_keys
        if press["fate"] == "deep" and press["key"] in self.combos:
            combo = self.combos[press["key"]]
            for c in combo:
                output.send(c, 1)
            for c in reversed(combo):
                output.send(c, 0)
            return
        press["code"] = code
        if press["fate"] == "deep":
            shift = BY_NAME["LSHIFT"][2]
            output.send(shift, 1)
            output.send(code, 1)
            output.send(shift, 0)
        else:
            output.send(code, 1)

    def release(self):
        self.update({}, 0.0)
        self.stop_holding()

    def close(self):
        self.release()
        if self.virtual_keys:
            self.virtual_keys.close()


# ---- lights ----------------------------------------------------------------------------------------------------------

class Lights:
    """Makes each key glow with how far it is pressed, and fade after you let go."""

    def __init__(self, color=(255, 90, 0), base=(0, 0, 0), fade=0.25, hz=30.0):
        self.color, self.base, self.fade, self.period = color, base, fade, 1.0 / hz
        self.levels, self.shown = {}, {}
        self.last = 0.0

    def start(self, kb):
        self.levels, self.shown = {}, {}
        kb.set_all_colors(*self.base)

    def update(self, kb, depths, now):
        elapsed = now - self.last
        if elapsed < self.period:
            return
        self.last = now
        changes = {}
        for key, mm in depths.items():
            level = mm / TRAVEL_MM
            if self.fade > 0:
                level = max(level, self.levels.get(key, 0.0) - elapsed / self.fade)
            self.levels[key] = level
            color = tuple(int(b + (c - b) * level) for b, c in zip(self.base, self.color))
            if color != self.shown.get(key, self.base):
                self.shown[key] = color
                changes[key] = color
        if changes:
            kb.set_colors(changes)

    def stop(self, kb):
        try:
            kb.clear_colors()
        except Disconnected:
            pass  # a replug resets the lighting anyway


class GlowOverNormal(Lights):
    """Leaves the keyboard's normal lighting alone: a pressed key glows brighter the deeper it goes, then goes back to normal.

    The firmware can only release every key at once, so letting go of one releases them all and re-sends the ones still held.
    A key's normal colour can't be read, so a press starts from a dim version of `color` rather than blending from it.
    """
    FLOOR = 0.25  # brightness of the lightest touch, as a share of `color`

    def __init__(self, color=(255, 255, 255), fade=0.1, hz=30.0):
        super().__init__(color, (0, 0, 0), fade, hz)

    def start(self, kb):
        self.levels, self.shown = {}, {}
        kb.clear_colors()

    def update(self, kb, depths, now):
        elapsed = now - self.last
        if elapsed < self.period:
            return
        self.last = now
        wanted = {}
        for key, mm in depths.items():
            level = mm / TRAVEL_MM
            if self.fade > 0:
                level = max(level, self.levels.get(key, 0.0) - elapsed / self.fade)
            self.levels[key] = level
            if level > 0:
                strength = self.FLOOR + (1 - self.FLOOR) * level
                wanted[key] = tuple(int(c * strength) for c in self.color)
        if set(self.shown) - set(wanted):
            kb.clear_colors()
            self.shown = {}
        changes = {k: v for k, v in wanted.items() if self.shown.get(k) != v}
        if changes:
            kb.set_colors(changes)
        self.shown = wanted


# ---- how you type ----------------------------------------------------------------------------------------------------

class Press(NamedTuple):
    key: str
    down: float
    up: float
    peak: float  # deepest point in mm
    speed: float  # fastest going down, in mm/s


class _PressLog:
    """Turns key depths into finished presses."""

    def __init__(self, down=0.5, up=0.3):
        self.down, self.up = down, up
        self.last, self.open = {}, {}

    def update(self, depths, now):
        finished = []
        for key, mm in depths.items():
            last_mm, last_time = self.last.get(key, (mm, now))
            self.last[key] = (mm, now)
            press = self.open.get(key)
            if press is None:
                if mm >= self.down:
                    speed = (mm - last_mm) / (now - last_time) if now > last_time else 0.0
                    self.open[key] = {"down": now, "peak": mm, "speed": max(speed, 0.0)}
            else:
                press["peak"] = max(press["peak"], mm)
                if now > last_time:
                    press["speed"] = max(press["speed"], (mm - last_mm) / (now - last_time))
                if mm <= self.up:
                    finished.append(Press(key, press["down"], now, press["peak"], press["speed"]))
                    del self.open[key]
        return finished


TYPEPRINT_PHRASE = "pressure never lies"  # fixed on purpose: this file must never hold a real password
TYPEPRINT_SAMPLES = 8
TYPEPRINT_FLOOR = 0.12  # the least spread any measurement is allowed, so one lucky match can't dominate a score


def _config_path(name):
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return os.path.join(base, "apex-analog", name)


def _state_path(name):
    base = os.environ.get("XDG_STATE_HOME") or os.path.expanduser("~/.local/state")
    return os.path.join(base, "apex-analog", name)


def _write_private(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    temporary = path + ".tmp"
    with open(os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as f:
        json.dump(data, f)
    os.replace(temporary, path)


def typing_features(presses):
    """One attempt at the phrase -> numbers: for each key its depth, speed, how long it was held, and the gap since the last key."""
    presses = sorted(presses, key=lambda p: p.down)
    features = []
    for i, press in enumerate(presses):
        features += [press.peak, math.log(max(press.speed, 0.0) + 1), math.log(press.up - press.down + 0.001)]
        features.append(math.log(press.down - presses[i - 1].down + 0.001) if i else 0.0)
    return features


def _typeprint_template(samples):
    columns = list(zip(*samples))
    return {"means": [statistics.fmean(c) for c in columns], "spreads": [max(statistics.pstdev(c), TYPEPRINT_FLOOR) for c in columns]}


def typeprint_score(template, sample):
    """Average distance from your usual typing, in spreads. Lower is more like you."""
    return statistics.fmean(abs(x - m) / s for x, m, s in zip(sample, template["means"], template["spreads"]))


def build_typeprint(samples):
    """A template plus a limit set by how much your own attempts differ, testing each one against a template of the others."""
    own = [typeprint_score(_typeprint_template(samples[:i] + samples[i + 1:]), s) for i, s in enumerate(samples)]
    return {**_typeprint_template(samples), "limit": max(own) * 1.25, "own_scores": own}


def _bins(mm):
    return min(40, max(0, int(mm * 10)))


class TypingProfile:
    """Per-key totals of how you press. Never the order of keys, so it can't be read back as text."""

    def __init__(self, data=None):
        self.keys = (data or {}).get("keys", {})

    def add(self, press, actuates_at=None):
        stats = self.keys.setdefault(press.key, {"count": 0, "bins": [0] * 41, "speed": 0.0, "held": 0.0, "near_misses": 0})
        stats["count"] += 1
        stats["bins"][_bins(press.peak)] += 1
        stats["speed"] += press.speed
        stats["held"] += press.up - press.down
        if actuates_at is not None and press.peak < actuates_at:
            stats["near_misses"] += 1

    @staticmethod
    def _percentile(bins, fraction):
        total, seen = sum(bins), 0
        for i, count in enumerate(bins):
            seen += count
            if total and seen >= fraction * total:
                return i / 10
        return 0.0

    def report(self, actuation=None):
        if not self.keys:
            return ["Nothing recorded yet. Run apex-analog profile and type for a while."]
        bins = [sum(s["bins"][i] for s in self.keys.values()) for i in range(41)]
        total = sum(bins)
        near = sum(s["near_misses"] for s in self.keys.values())
        light, typical = self._percentile(bins, 0.05), self._percentile(bins, 0.5)
        bottom = sum(bins[37:]) / total
        lines = [f"{total} presses on {len(self.keys)} keys.",
                 f"Typical press reaches {typical:.1f} mm. {bottom:.0%} bottom out. Your lightest 5% stop by {light:.1f} mm."]
        if actuation:
            lines.append(f"{near} presses ({near / total:.1%}) stopped short of your {actuation:.2f} mm actuation point, so the keyboard ignored them.")
            if near / total > 0.02:
                lines.append(f"That's a lot of missed presses: an actuation point near {max(0.2, light * 0.8):.1f} mm would catch them.")
            elif light > actuation + 0.5:
                lines.append(f"Your presses comfortably clear it, so you have room to raise actuation toward {light * 0.8:.1f} mm for fewer accidental presses.")
            else:
                lines.append("Your actuation point suits how you type.")
        lines += ["", f"{'key':8}{'presses':>8}{'typical mm':>12}{'mm/s':>8}{'held ms':>9}"]
        for key, s in sorted(self.keys.items(), key=lambda kv: -kv[1]["count"])[:20]:
            lines.append(f"{key:8}{s['count']:8d}{self._percentile(s['bins'], 0.5):12.1f}{s['speed'] / s['count']:8.0f}{1000 * s['held'] / s['count']:9.0f}")
        return lines


def key_health(offset, noise, span, typical_span):
    """What looks wrong with one key: how far it rests from its calibrated zero, how much its reading jitters, and its range."""
    problems = []
    if offset > 80:
        problems.append(f"rests {offset:.0f} counts high (held down, dirty, or worn)")
    if offset < -30:
        problems.append(f"rests {-offset:.0f} counts low (needs recalibrating?)")
    if noise > 6:
        problems.append(f"noisy reading ({noise:.1f} counts)")
    if abs(span - typical_span) > 0.15 * typical_span:
        problems.append(f"range {span} counts, others are about {typical_span}")
    return problems


# ---- outputs for other programs --------------------------------------------------------------------------------------

def _host_port(text):
    host, _, port = text.rpartition(":")
    if not host or not port.isdigit():
        raise ValueError(f"{text!r} should look like 127.0.0.1:9000")
    return host, int(port)


def _is_loopback(host):
    return host == "localhost" or host.startswith("127.") or host == "::1"


class UdpSink:
    """One JSON datagram per update."""

    def __init__(self, target):
        self.target = _host_port(target)
        self.socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

    def send(self, message):
        try:
            self.socket.sendto(json.dumps(message).encode(), self.target)
        except OSError:
            pass


def osc_message(address, *values):
    """An OSC 1.0 message of floats and strings."""
    def padded(data):
        return data + b"\0" * (4 - len(data) % 4)
    tags = "," + "".join("f" if isinstance(v, (int, float)) else "s" for v in values)
    body = b"".join(struct.pack(">f", v) if isinstance(v, (int, float)) else padded(v.encode()) for v in values)
    return padded(address.encode()) + padded(tags.encode()) + body


class OscSink:
    """/apex/W <mm> whenever a key's depth changes; /apex/W/down and /apex/W/up <mm/s> in --events mode."""

    def __init__(self, target):
        self.target = _host_port(target)
        self.socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.last = {}

    def send(self, message):
        try:
            if "event" in message:
                self.socket.sendto(osc_message(f"/apex/{message['key']}/{message['event']}", message["velocity"]), self.target)
                return
            for key in set(self.last) | set(message):
                value = message.get(key, 0.0)
                if self.last.get(key) != value:
                    self.last[key] = value
                    self.socket.sendto(osc_message(f"/apex/{key}", value), self.target)
        except OSError:
            pass


OVERLAY_PAGE = r"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>apex-analog overlay</title><style>
html, body { margin: 0; background: transparent; font: 600 15px system-ui, sans-serif; color: #f3f3f3; }
#board { display: inline-flex; flex-direction: column; gap: 5px; padding: 10px; }
.row { display: flex; gap: 5px; }
.key { position: relative; height: 54px; border-radius: 7px; overflow: hidden; background: rgba(18, 20, 24, .78); box-shadow: inset 0 0 0 1px rgba(255, 255, 255, .22); }
.key.absent { visibility: hidden; }
.fill { position: absolute; left: 0; right: 0; bottom: 0; height: 0; background: #e8862e; }
.key.deep .fill { background: #f6c343; }
.key.deep .name, .key.deep .mm { color: #16181c; text-shadow: none; }
.name, .mm { position: absolute; left: 7px; text-shadow: 0 1px 2px #000; }
.name { top: 5px; }
.mm { bottom: 4px; font-size: 12px; font-variant-numeric: tabular-nums; }
</style></head><body><div id="board"></div><script>
const ROWS = [
  [["`",1],["1",1],["2",1],["3",1],["4",1],["5",1],["6",1],["7",1],["8",1],["9",1],["0",1],["-",1],["=",1],["BKSP",2]],
  [["TAB",1.5],["Q",1],["W",1],["E",1],["R",1],["T",1],["Y",1],["U",1],["I",1],["O",1],["P",1],["[",1],["]",1],["\\",1.5]],
  [["CAPS",1.75],["A",1],["S",1],["D",1],["F",1],["G",1],["H",1],["J",1],["K",1],["L",1],[";",1],["'",1],["ENTER",2.25]],
  [["LSHIFT",2.25],["Z",1],["X",1],["C",1],["V",1],["B",1],["N",1],["M",1],[",",1],[".",1],["/",1],["RSHIFT",2.75]],
  [["LCTRL",1.25],["LGUI",1.25],["LALT",1.25],["SPACE",6.25],["RALT",1.25],["RGUI",1.25],["SS",1.25],["RCTRL",1.25]],
];
const LABELS = {LSHIFT: "SHIFT", RSHIFT: "SHIFT", LCTRL: "CTRL", RCTRL: "CTRL", LALT: "ALT", RALT: "ALT", LGUI: "WIN", RGUI: "WIN", BKSP: "BACK"};
const UNIT = 54, GAP = 5, keys = {}, board = document.getElementById("board");
for (const row of ROWS) {
  const line = document.createElement("div");
  line.className = "row";
  for (const [name, width] of row) {
    const key = document.createElement("div");
    key.className = "key absent";
    key.style.width = (width * UNIT + (width - 1) * GAP) + "px";
    key.innerHTML = '<div class="fill"></div><div class="name"></div><div class="mm"></div>';
    key.querySelector(".name").textContent = LABELS[name] || name;
    line.appendChild(key);
    keys[name] = key;
  }
  board.appendChild(line);
}
function connect() {
  const socket = new WebSocket("ws://" + location.host);
  socket.onmessage = event => {
    const depths = JSON.parse(event.data);
    for (const [name, mm] of Object.entries(depths)) {
      const key = keys[name];
      if (!key) continue;
      key.classList.remove("absent");
      key.classList.toggle("deep", mm >= 3.5);
      key.querySelector(".fill").style.height = (mm / 4 * 100) + "%";
      key.querySelector(".mm").textContent = mm > 0 ? mm.toFixed(1) : "";
    }
  };
  socket.onclose = () => setTimeout(connect, 1000);
}
connect();
</script></body></html>"""

WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


def ws_accept_value(key):
    return base64.b64encode(hashlib.sha1((key + WS_GUID).encode()).digest()).decode()


def origin_allowed(origin):
    """Web pages could otherwise connect and read keys. Only pages served from this machine may; programs send no Origin."""
    if origin is None:
        return True
    host = origin.split("://", 1)[-1].split("/", 1)[0].rsplit(":", 1)[0]
    return host in ("localhost", "127.0.0.1", "[::1]")


class ServeSink:
    """WebSocket and plain HTTP on 127.0.0.1 only: connect a socket for live updates, or GET for the latest one."""

    def __init__(self, port):
        self.server = socket.socket()
        self.server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.server.bind(("127.0.0.1", port))
        self.server.listen(8)
        self.clients, self.lock, self.latest = [], threading.Lock(), {}
        threading.Thread(target=self._accept, daemon=True).start()

    def _accept(self):
        while True:
            try:
                client, _ = self.server.accept()
            except OSError:
                return
            threading.Thread(target=self._greet, args=(client,), daemon=True).start()

    def _greet(self, client):
        try:
            client.settimeout(2)
            request = b""
            while b"\r\n\r\n" not in request and len(request) < 8192:
                chunk = client.recv(1024)
                if not chunk:
                    client.close()
                    return
                request += chunk
            headers = {}
            for line in request.decode(errors="replace").split("\r\n")[1:]:
                name, _, value = line.partition(":")
                headers[name.strip().lower()] = value.strip()
            if not origin_allowed(headers.get("origin")):
                client.sendall(b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
                client.close()
                return
            if "sec-websocket-key" not in headers:
                path = request.split(b" ", 2)[1] if request.count(b" ") >= 2 else b"/"
                body, kind = (OVERLAY_PAGE.encode(), b"text/html") if path.split(b"?")[0] == b"/overlay" else (json.dumps(self.latest).encode(), b"application/json")
                client.sendall(b"HTTP/1.1 200 OK\r\nContent-Type: " + kind + b"\r\nConnection: close\r\nContent-Length: "
                               + str(len(body)).encode() + b"\r\n\r\n" + body)
                client.close()
                return
            client.sendall(("HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                            f"Sec-WebSocket-Accept: {ws_accept_value(headers['sec-websocket-key'])}\r\n\r\n").encode())
            client.setblocking(False)
            with self.lock:
                self.clients.append(client)
        except OSError:
            client.close()

    def send(self, message):
        self.latest = message
        data = json.dumps(message).encode()
        header = bytes([0x81, len(data)]) if len(data) < 126 else bytes([0x81, 126]) + struct.pack(">H", len(data))
        with self.lock:
            for client in list(self.clients):
                try:
                    if client.send(header + data) != len(header) + len(data):
                        raise OSError("slow client")
                except OSError:
                    self.clients.remove(client)
                    client.close()


# ---- command line ----------------------------------------------------------------------------------------------------

def _say(message):
    print(message, file=sys.stderr, flush=True)


class _Screen:
    """Full-screen drawing that leaves the terminal as it was. Keys pressed meanwhile are thrown away, not run at the prompt."""

    def __init__(self):
        self.active = sys.stdout.isatty() and sys.stdin.isatty()

    def __enter__(self):
        if self.active:
            self.saved = termios.tcgetattr(0)
            quiet = termios.tcgetattr(0)
            quiet[3] &= ~(termios.ECHO | termios.ICANON)
            termios.tcsetattr(0, termios.TCSANOW, quiet)
            sys.stdout.write("\033[?1049h\033[?25l")
        return self

    def draw(self, lines):
        if self.active:
            sys.stdout.write("\033[H" + "\033[K\n".join(lines) + "\033[K\033[J")
            sys.stdout.flush()

    def __exit__(self, *_):
        if self.active:
            sys.stdout.write("\033[?25h\033[?1049l")
            sys.stdout.flush()
            termios.tcflush(0, termios.TCIFLUSH)
            termios.tcsetattr(0, termios.TCSANOW, self.saved)


def _bar(mm, mark=None, width=40):
    filled = int(width * mm / TRAVEL_MM)
    cells = ["#" if i < filled else " " for i in range(width)]
    if mark is not None:
        cells[min(width - 1, int(width * mark / TRAVEL_MM))] = "|" if mm < mark else "#"
    return "".join(cells)


def cmd_bars(args):
    with Keyboard() as kb, _Screen() as screen:
        if not screen.active:
            raise ApexError("The live bars need a terminal. For piping, use: apex-analog stream")
        settings = kb.actuation()
        scans, rate, last = 0, 0.0, time.monotonic()
        for depths in kb.stream(args.hz or 60, args.keys):
            scans += 1
            now = time.monotonic()
            if now - last >= 1:
                rate, scans, last = scans / (now - last), 0, now
            pressed = sorted(((mm, name) for name, mm in depths.items() if mm > 0), reverse=True)
            lines = [f"apex-analog  {len(depths)} keys, {rate:.0f} updates/s, | marks where the key actuates. Ctrl-C quits.", ""]
            for mm, name in pressed[:16]:
                lines.append(f"{name:7}|{_bar(mm, settings[name]['press'])}| {mm:3.1f} mm")
            if not pressed:
                lines.append("Press a key slowly.")
            screen.draw(lines)


def _network_sinks(args):
    sinks = []
    if args.udp or args.osc or args.serve:
        targets = [t for t in (args.udp, args.osc) if t]
        if not args.keys:
            raise ValueError("Network outputs need --keys, for example --keys WASD, so everything you type isn't sent out. "
                             "--keys all sends every key, on this machine only.")
        if args.keys == "all" and not all(_is_loopback(_host_port(t)[0]) for t in targets):
            raise ValueError("--keys all would send everything you type off this machine, passwords included. List the keys you want.")
    if args.udp:
        sinks.append(UdpSink(args.udp))
    if args.osc:
        sinks.append(OscSink(args.osc))
    if args.serve:
        sinks.append(ServeSink(args.serve))
    return sinks


def _selected_keys(args):
    return None if args.keys in (None, "all") else key_names(args.keys)


def _loop(hz, keys, holder, on_frame, lights=None):
    """Read, map, send. Comes back after an unplug or a suspend, and lets go of every output while the keyboard is away."""
    Keyboard().close()  # a problem at the start should be reported, not waited on
    waiting = False
    while True:
        try:
            with Keyboard() as kb:
                if waiting:
                    _say("Keyboard is back.")
                    waiting = False
                if holder:
                    missing = [k for k in holder.keys if k not in kb.keys]
                    if missing:
                        raise ApexError(f"No sensor fitted on this keyboard for: {' '.join(missing)}")
                    holder.attach()
                try:
                    if lights:
                        lights.start(kb)
                    last = time.monotonic()
                    for depths in kb.stream(hz, keys):
                        now = time.monotonic()
                        if holder:
                            holder.update(depths, now - last)
                        last = now
                        if lights:
                            lights.update(kb, depths, now)
                        on_frame(depths, now)
                finally:
                    if lights:
                        lights.stop(kb)
        except (Disconnected, NotFound, AccessDenied) as e:
            if isinstance(e, AccessDenied) and not waiting:
                raise
            if holder:
                holder.release()
            if not waiting:
                _say("Keyboard went away. Waiting for it.")
                waiting = True
            time.sleep(1)


def _frame_message(depths, explicit):
    return {k: round(v, 2) for k, v in depths.items() if explicit or v > 0}


def cmd_stream(args):
    explicit = _selected_keys(args)
    sinks = _network_sinks(args)
    tracker = _Tracker(args.down, args.up) if args.events else None

    def send(message):
        if not sinks:
            print(json.dumps(message), flush=True)
        for sink in sinks:
            sink.send(message)

    def on_frame(depths, now):
        if tracker:
            for e in tracker.update(depths, now):
                send({"key": e.key, "event": e.kind, "mm": e.mm, "velocity": e.velocity, "t": round(e.time, 4)})
        else:
            send(_frame_message(depths, explicit))

    _loop(args.hz or (200 if tracker else 60), explicit, None, on_frame)


def _rgb(text):
    value = text.lstrip("#")
    if not re.fullmatch(r"[0-9a-fA-F]{6}", value):
        raise ValueError(f"{text!r} should be a colour like FF5A00")
    return tuple(bytes.fromhex(value))


def _lights_from(args):
    if args.keep:
        return GlowOverNormal(_rgb(args.color or "FFFFFF"), 0.1 if args.fade is None else args.fade)
    return Lights(_rgb(args.color or "FF5A00"), _rgb(args.base), 0.25 if args.fade is None else args.fade)


def cmd_lights(args):
    if args.off:
        with Keyboard() as kb:
            kb.clear_colors()
        print("Back to your normal lighting.")
        return 0
    lights = _lights_from(args)
    keys = key_names(args.keys) if args.keys else None
    with _Screen() as screen:
        screen.draw(["apex-analog lights", "", ("Your normal lighting stays on, and keys glow as you press them." if args.keep else "The keyboard goes dark, then keys glow as you press them.")
                     + " Ctrl-C quits and restores your normal lighting."])
        _loop(args.hz or 60, keys, None, lambda depths, now: None, lights)


def _map_lines(mapper, rate):
    lines = [f"apex-analog  {rate:.0f} updates/s  " + " ".join(m.text for m in mapper.maps), ""]
    for name, value in mapper.values.items():
        bare = name.split(":")[-1]
        if bare in Gamepad.STICKS:
            half = 20
            left = int(half * max(0.0, -value))
            right = int(half * max(0.0, value))
            lines.append(f"{name:10}|{' ' * (half - left)}{'#' * left}|{'#' * right}{' ' * (half - right)}| {value:+.2f}")
        elif bare in Gamepad.TRIGGERS or bare in Pedals.AXES:
            n = int(41 * value)
            lines.append(f"{name:10}|{'#' * n}{' ' * (41 - n)}| {value:.2f}")
        else:
            lines.append(f"{name:10}{'ON' if value else 'off'}")
    if mapper.mouse:
        lines.append("mouse is live")
    if mapper.key_outputs:
        lines.append("keystrokes are live")
    return lines + _holder_lines(mapper)


def _holder_lines(holder):
    if holder.paused:
        return ["", "PAUSED: your keys type normally. Press Pause to resume."]
    if holder.swallow:
        return ["", "These keys won't type while this runs. Pause key pauses. Ctrl-C quits."]
    return ["", "Ctrl-C quits."]


def _drive(holder, hz, read, lines, sinks=(), explicit=None, lights=None):
    """Run a key-driven mode: read the keyboard, feed the mode and any network outputs, and keep the screen up to date."""
    state = {"scans": 0, "rate": 0.0, "last": time.monotonic(), "drawn": 0.0}
    try:
        with _Screen() as screen:
            def on_frame(depths, now):
                for sink in sinks:
                    sink.send(_frame_message({k: v for k, v in depths.items() if not explicit or k in explicit}, explicit))
                state["scans"] += 1
                if now - state["last"] >= 1:
                    state.update(rate=state["scans"] / (now - state["last"]), scans=0, last=now)
                if now - state["drawn"] >= 0.05:
                    state["drawn"] = now
                    screen.draw(lines(holder, state["rate"]))
            _loop(hz, read, holder, on_frame, lights)
    finally:
        holder.close()


def cmd_map(args):
    sinks = _network_sinks(args)
    explicit = _selected_keys(args)
    maps = args.maps or (["drive"] if args.command == "gamepad" else [])
    Keyboard().close()  # say what's wrong with the keyboard before creating virtual devices
    mapper = Mapper(maps, args.deadzone, args.full, args.curve, args.rapid, args.mouse_speed, args.wheel_speed,
                    args.tap_rate, args.run_rate, not args.no_swallow, args.pause_key)
    read = sorted({*mapper.keys, *(explicit or [])}, key=lambda n: BY_NAME[n][0]) if args.keys != "all" else None
    for player, pad in mapper.pads.items():
        _say(f"Virtual controller {player} ready: {pad.path}")
    if mapper.pedals:
        _say(f"Virtual pedals ready: {mapper.pedals.path}. They have no default bindings, so bind them in your game once.")
    _drive(mapper, args.hz or 120, read, _map_lines, sinks, explicit, _lights_from(args) if args.lights else None)


def _midi_lines(instrument, rate):
    playing = " ".join(note_name(n) for n in sorted(instrument.sounding)) or "-"
    lines = [f"apex-analog midi  {rate:.0f} updates/s  ALSA port {instrument.midi.address} \"Apex Analog\"", "",
             f"Playing: {playing}",
             f"Sustain {instrument.cc_values.get('SPACE', 0):3d}   Mod wheel {instrument.cc_values.get('LSHIFT', 0):3d}   Pitch bend {instrument.bend:+5d}",
             "", "Z row and Q row are piano keys. SPACE sustains, LSHIFT is the mod wheel, TAB and CAPS bend pitch."]
    return lines + _holder_lines(instrument)


def _parse_pairs(items, label):
    out = {}
    for item in items or []:
        key, separator, value = item[1:].partition("=")
        if not separator or not value:
            raise ValueError(f"{label} should look like J=something, not {item!r}")
        out[item[0] + key] = value
    return out


def cmd_midi(args):
    Keyboard().close()
    ccs = {key: int(value) for key, value in _parse_pairs(args.cc, "--cc").items()}
    instrument = MidiInstrument(args.layout, args.octave, args.channel, args.note_at, args.aftertouch, args.soft, args.hard, ccs,
                                swallow=not args.no_swallow, pause_key=args.pause_key)
    _say(f"MIDI port ready: {instrument.midi.address} \"Apex Analog\". Connect it from your synth or DAW, or with aconnect.")
    _drive(instrument, args.hz or 300, instrument.keys, _midi_lines, lights=_lights_from(args) if args.lights else None)


def _type_lines(typer, rate):
    lines = [f"apex-analog type  {rate:.0f} updates/s", "",
             f"Press light for lowercase. Past {typer.deep:.1f} mm gives a capital or the other symbol on that key."]
    if typer.combos:
        lines.append("Deep combos: " + " ".join(typer.combos))
    return lines + _holder_lines(typer)


def cmd_type(args):
    Keyboard().close()
    typer = PressureTyper(args.at, args.deep, args.hold, _parse_pairs(args.combo, "--combo"), args.pause_key)
    _drive(typer, args.hz or 300, typer.keys, _type_lines)


def _capture_phrase(kb, screen, heading):
    wanted = ["SPACE" if c == " " else c.upper() for c in TYPEPRINT_PHRASE]
    log, presses = _PressLog(), []

    def text():
        ordered = sorted(presses, key=lambda p: p.down)
        return "".join(" " if p.key == "SPACE" else p.key.lower() for p in ordered)

    screen.draw([heading, "", f"Type:  {TYPEPRINT_PHRASE}", f"       {text()}", "", "Then press Enter. Type the way you normally type. Ctrl-C quits."])
    for depths in kb.stream(400, sorted({*wanted, "ENTER"}, key=lambda n: BY_NAME[n][0])):
        finished = log.update(depths, time.monotonic())
        for press in finished:
            if press.key == "ENTER":
                ordered = sorted(presses, key=lambda p: p.down)
                return typing_features(ordered) if [p.key for p in ordered] == wanted else None
            presses.append(press)
        if finished:
            screen.draw([heading, "", f"Type:  {TYPEPRINT_PHRASE}", f"       {text()}", "", "Then press Enter. Type the way you normally type. Ctrl-C quits."])


def cmd_typeprint(args):
    path = _config_path("typeprint.json")
    if args.action == "forget":
        if os.path.exists(path):
            os.remove(path)
        print("Forgotten.")
        return 0
    if args.action == "check" and not os.path.exists(path):
        raise ApexError("Nothing to compare against yet. Run: apex-analog typeprint enroll")
    with Keyboard() as kb, _Screen() as screen:
        if not screen.active:
            raise ApexError("typeprint needs a terminal.")
        if args.action == "enroll":
            samples = []
            while len(samples) < TYPEPRINT_SAMPLES:
                sample = _capture_phrase(kb, screen, f"Attempt {len(samples) + 1} of {TYPEPRINT_SAMPLES}")
                if sample is not None:
                    samples.append(sample)
            result = build_typeprint(samples)
        else:
            sample = None
            while sample is None:
                sample = _capture_phrase(kb, screen, "Prove it's you")
            with open(path) as f:
                stored = json.load(f)
            score = typeprint_score(stored, sample)
    if args.action == "enroll":
        _write_private(path, result)
        print(f"Saved your typing print. Your own attempts scored {min(result['own_scores']):.2f} to {max(result['own_scores']):.2f}, so the limit is {result['limit']:.2f}.")
        print("Now try apex-analog typeprint check, then see whether anyone else can pass it.")
        return 0
    matched = score <= stored["limit"]
    print(f"{'Match' if matched else 'No match'}: scored {score:.2f}, limit {stored['limit']:.2f}. Lower is more like you. This is an experiment, not security.")
    return 0 if matched else 1


def cmd_profile(args):
    path = _state_path("typing-profile.json")
    if args.reset:
        if os.path.exists(path):
            os.remove(path)
        print("Cleared.")
        return 0
    data = {}
    if os.path.exists(path):
        with open(path) as f:
            data = json.load(f)
    profile = TypingProfile(data)
    if args.report:
        with Keyboard() as kb:
            settings = kb.actuation()
        print("\n".join(profile.report(statistics.median(s["press"] for s in settings.values()))))
        return 0
    log = _PressLog()
    started, saved = time.monotonic(), time.monotonic()
    with Keyboard() as kb:
        settings = kb.actuation()
        _say("Recording how you press, as per-key totals only, never the order of keys. Type normally. Ctrl-C stops and shows the report.")
        try:
            for depths in kb.stream(args.hz or 150):
                now = time.monotonic()
                for press in log.update(depths, now):
                    profile.add(press, settings.get(press.key, {}).get("press"))
                if now - saved >= 30:
                    _write_private(path, {"keys": profile.keys})
                    saved = now
                if args.minutes and now - started >= args.minutes * 60:
                    break
        except KeyboardInterrupt:
            pass
    _write_private(path, {"keys": profile.keys})
    print("\n".join(profile.report(statistics.median(s["press"] for s in settings.values()))))
    return 0


def cmd_health(args):
    with Keyboard() as kb:
        samples = {k: [] for k in kb.keys}
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            for key, value in kb.raw().items():
                samples[key].append(value)
        spans = {k: top - rest for k, (top, rest) in kb._calibration.items()}
        typical = int(statistics.median(spans.values()))
        rows = []
        for key, values in samples.items():
            top, rest = kb._calibration[key]
            offset, noise = statistics.fmean(values) - rest, statistics.pstdev(values)
            rows.append((key, offset, noise, spans[key], key_health(offset, noise, spans[key], typical)))
        peaks = {}
        if args.press:
            print(f"Press every key all the way down, one at a time. {args.press:.0f} seconds.", flush=True)
            deadline = time.monotonic() + args.press
            for depths in kb.stream(150):
                for key, mm in depths.items():
                    peaks[key] = max(peaks.get(key, 0.0), mm)
                if time.monotonic() > deadline:
                    break
    bad = [r for r in rows if r[4]]
    print(f"{len(rows)} keys checked. Rest offset {min(r[1] for r in rows):.0f} to {max(r[1] for r in rows):.0f} counts, "
          f"noise up to {max(r[2] for r in rows):.1f}, typical range {typical}.")
    if bad:
        print(f"{len(bad)} to look at:")
        for key, _, _, _, problems in bad:
            print(f"  {key:8}{'; '.join(problems)}")
    else:
        print("Nothing looks wrong.")
    if args.all:
        print(f"\n{'key':8}{'offset':>8}{'noise':>7}{'range':>7}")
        for key, offset, noise, span, _ in rows:
            print(f"{key:8}{offset:8.0f}{noise:7.1f}{span:7d}")
    if args.press:
        untouched = [k for k in samples if peaks.get(k, 0.0) == 0.0]
        short = {k: v for k, v in peaks.items() if 0.0 < v < 3.5}
        print(f"\nPressed {len(samples) - len(untouched)} of {len(samples)} keys.")
        if short:
            print("Didn't reach the bottom: " + ", ".join(f"{k} {v:.1f} mm" for k, v in sorted(short.items(), key=lambda kv: kv[1])))
        if untouched:
            print("Not pressed: " + " ".join(untouched))
    return 0


def cmd_overlay(args):
    every = args.keys == "all"
    if every:
        _say("Warning: --keys all shows everything you type, passwords included, to anyone watching your stream.")
    explicit = key_names(args.keys) if not every else None
    sink = ServeSink(args.port)
    _say(f"Overlay ready. Add this as an OBS browser source: http://127.0.0.1:{args.port}/overlay")

    def on_frame(depths, now):
        sink.send({k: round(v, 2) for k, v in depths.items()})

    _loop(args.hz or 60, explicit, None, on_frame)


def cmd_info(args):
    with Keyboard() as kb:
        settings = kb.actuation()
        depths = kb.read()
        report = {
            "keyboard": f"{VENDOR}:{PRODUCT}", "firmware": kb.firmware, "node": kb.node,
            "virtual_devices": os.access("/dev/uinput", os.W_OK),
            "can_stop_keys_typing": all(os.access(n, os.R_OK) for n in _physical_key_nodes()),
            "oled_actuation_icon": kb.actuation_icon(),
            "keys": {k: {"mm": round(depths[k], 2), **settings[k]} for k in kb.keys},
        }
    if args.json:
        print(json.dumps(report))
        return
    print(f"Apex Pro Gen 3 ({report['keyboard']}), firmware {report['firmware']}, {report['node']}, {len(report['keys'])} keys with sensors")
    print(f"Virtual controller, mouse, keystrokes: {'ok' if report['virtual_devices'] else 'no access, run ./install.sh'}")
    print(f"Stopping mapped keys from typing:      {'ok' if report['can_stop_keys_typing'] else 'no access, run ./install.sh'}")
    print(f"OLED ACTUATION ICON setting:           {'on' if report['oled_actuation_icon'] else 'off'}")
    print()
    print(f"{'key':8}{'now':>6}{'press':>7}{'release':>9}{'mode':>6}   (mm)")
    for name, k in report["keys"].items():
        print(f"{name:8}{k['mm']:6.1f}{k['press']:7.2f}{k['release']:9.2f}{k['mode']:6d}")


def cmd_actuation(args):
    if args.mm is None and args.keys is not None:
        raise ValueError("Say how deep too, like: apex-analog actuation W 1.0")
    with Keyboard() as kb:
        if args.keys is not None:
            names = list(kb.keys) if args.keys == "all" else key_names(args.keys)
            missing = [n for n in names if n not in kb.keys]
            if missing:
                raise ValueError(f"No sensor fitted on this keyboard for: {' '.join(missing)}")
            level = kb.set_actuation(names, args.mm)
            time.sleep(0.1)
            changed = [n for n in names if n != "LGUI"]
            _, press, release = actuation_level(args.mm)
            settings = kb.actuation(changed)
            wrong = [n for n in changed if (settings[n]["press_raw"], settings[n]["release_raw"]) != (press, release)]
            if wrong:
                raise ApexError(f"Sent, but these read back differently: {' '.join(wrong)}")
            print(f"Set {len(changed)} key{'s' if len(changed) != 1 else ''} to {level:.1f} mm and read it back.")
            if "LGUI" in names:
                print("LGUI skipped: this command can't change it.")
        settings = kb.actuation()
    groups = {}
    for name, value in settings.items():
        groups.setdefault(value["press"], []).append(name)
    for press, names in sorted(groups.items()):
        print(f"{press:.2f} mm: " + ("every other key" if len(names) > 30 else " ".join(names)))
    return 0


def _add_light_options(parser):
    parser.add_argument("--keep", action="store_true", help="keep your normal lighting: pressed keys glow white, then go back to normal")
    parser.add_argument("--color", metavar="RRGGBB", help="colour of a key at full depth (default FF5A00, or FFFFFF with --keep)")
    parser.add_argument("--base", default="000000", metavar="RRGGBB", help="colour of everything else (default 000000, dark)")
    parser.add_argument("--fade", type=float, metavar="SEC", help="how long a key takes to fade after you let go (default 0.25, or 0.1 with --keep)")


def _add_network_options(parser):
    parser.add_argument("--keys", help="which keys to send, like WASD or W,A,SPACE. Required for network outputs; 'all' sends every key on this machine only")
    parser.add_argument("--udp", metavar="HOST:PORT", help="send each update as a JSON datagram")
    parser.add_argument("--osc", metavar="HOST:PORT", help="send OSC: /apex/W <mm>")
    parser.add_argument("--serve", metavar="PORT", type=int, help="WebSocket and HTTP on 127.0.0.1:PORT")
    parser.add_argument("--hz", type=float, help="updates per second")


def build_parser():
    parser = argparse.ArgumentParser(prog="apex-analog", description="See how far every key is pressed on an Apex Pro Gen 3, and wire it to anything.")
    parser.add_argument("--version", action="version", version=__version__)
    parser.set_defaults(func=cmd_bars, command="bars", hz=None, keys=None)
    commands = parser.add_subparsers(dest="command", metavar="command")

    bars = commands.add_parser("bars", help="live bars in the terminal (the default)")
    bars.add_argument("--keys", help="only these keys, like WASD")
    bars.add_argument("--hz", type=float)
    bars.set_defaults(func=cmd_bars)

    for name, summary in (("gamepad", "turn keys into a virtual controller; with no rules, W S A D drive"),
                          ("map", "send keys to controllers, pedals, the mouse, keystrokes, or commands")):
        p = commands.add_parser(name, help=summary, fromfile_prefix_chars="@", description=summary, epilog=(
            "Rules look like KEY=OUTPUT, or KEY=OUTPUT@mm for on/off outputs. Sticks: lx- lx+ ly- ly+ rx- rx+ ry- ry+. Triggers: lt rt. "
            "Buttons: a b x y lb rb back start guide ls rs dpad-up dpad-down dpad-left dpad-right. Add p2: for a second controller. "
            "Pedals: clutch handbrake pedal3 pedal4 pedal5 pedal6. "
            "Mouse: mouse-x- mouse-x+ mouse-y- mouse-y+ wheel- wheel+ hwheel- hwheel+ mouse-left mouse-right mouse-middle. "
            "Keystroke: key:F13. Repeating keystroke, faster the deeper you press: tap:RIGHT. "
            "Desktop: volume+ volume- brightness+ brightness-. Any command: run:your-command. "
            f"Presets: {', '.join(PRESETS)}. Keep rules in a file, one per line, and pass @file."))
        p.add_argument("maps", nargs="*", metavar="RULE", help="W=rt, A=lx-, J=key:F13@2.0, or a preset")
        p.add_argument("--deadzone", type=float, default=0.3, metavar="MM", help="axis stays 0 above this depth (default 0.3)")
        p.add_argument("--full", type=float, default=3.5, metavar="MM", help="axis reaches 1 at this depth (default 3.5)")
        p.add_argument("--curve", type=float, default=1.0, help="above 1 gives finer control near the top of the press (default 1, try 1.5)")
        p.add_argument("--rapid", type=float, default=0.0, metavar="MM", help="on/off outputs re-trigger after moving this far either way")
        p.add_argument("--mouse-speed", type=float, default=1200.0, metavar="PX/S", help="cursor speed at full depth (default 1200)")
        p.add_argument("--wheel-speed", type=float, default=15.0, metavar="N/S", help="scroll speed at full depth (default 15)")
        p.add_argument("--tap-rate", type=float, default=20.0, metavar="N/S", help="tap: repeats at full depth (default 20)")
        p.add_argument("--run-rate", type=float, default=8.0, metavar="N/S", help="run: and desktop repeats at full depth (default 8)")
        p.add_argument("--no-swallow", action="store_true", help="let mapped keys keep typing as well")
        p.add_argument("--pause-key", default="PAUSE", metavar="KEY", help="key that pauses mapping so you can type (default PAUSE)")
        p.add_argument("--lights", action="store_true", help="also make these keys glow as you press them")
        _add_light_options(p)
        _add_network_options(p)
        p.set_defaults(func=cmd_map)

    s = commands.add_parser("stream", help="one JSON line per update, or send it over UDP, OSC, or WebSocket")
    s.add_argument("--events", action="store_true", help="send key presses and releases with their speed instead of depths")
    s.add_argument("--down", type=float, default=0.5, metavar="MM", help="--events: counts as pressed at this depth (default 0.5)")
    s.add_argument("--up", type=float, default=0.3, metavar="MM", help="--events: counts as released at this depth (default 0.3)")
    _add_network_options(s)
    s.set_defaults(func=cmd_stream)

    m = commands.add_parser("midi", help="play the keyboard as a MIDI instrument")
    m.add_argument("--layout", default="piano", choices=("piano", "grid"), help="piano: Z and Q rows shaped like piano keys. grid: every key a note, rows a fourth apart")
    m.add_argument("--octave", type=int, default=0, help="shift everything by octaves (default 0, Z is C3)")
    m.add_argument("--channel", type=int, default=1, help="MIDI channel 1-16")
    m.add_argument("--note-at", type=float, default=1.5, metavar="MM", help="depth where a note starts (default 1.5)")
    m.add_argument("--aftertouch", default="poly", choices=("poly", "channel", "off"), help="send hold depth per key, per channel, or not at all")
    m.add_argument("--soft", type=float, default=8.0, metavar="MM/S", help="press speed that gives the quietest note (default 8)")
    m.add_argument("--hard", type=float, default=160.0, metavar="MM/S", help="press speed that gives the loudest note (default 160)")
    m.add_argument("--cc", action="append", metavar="KEY=NUMBER", help="send a key's depth as a MIDI controller, like --cc F=74")
    m.add_argument("--no-swallow", action="store_true", help="let these keys keep typing as well")
    m.add_argument("--pause-key", default="PAUSE", metavar="KEY", help="key that pauses playing so you can type (default PAUSE)")
    m.add_argument("--lights", action="store_true", help="also make these keys glow as you press them")
    _add_light_options(m)
    m.add_argument("--hz", type=float, help="updates per second")
    m.set_defaults(func=cmd_midi)

    t = commands.add_parser("type", help="press deep for capitals or key combos, with no Shift")
    t.add_argument("--at", type=float, default=1.5, metavar="MM", help="depth where a key counts as pressed (default 1.5)")
    t.add_argument("--deep", type=float, default=3.2, metavar="MM", help="depth that counts as a deep press (default 3.2)")
    t.add_argument("--hold", type=float, default=0.15, metavar="SEC", help="a press held this long without going deep stays light (default 0.15)")
    t.add_argument("--combo", action="append", metavar="KEY=COMBO", help="deep press sends this instead of a capital, like --combo J=LCTRL+J or --combo K=F13")
    t.add_argument("--pause-key", default="PAUSE", metavar="KEY", help="key that pauses this so you can type normally (default PAUSE)")
    t.add_argument("--hz", type=float, help="updates per second")
    t.set_defaults(func=cmd_type)

    l = commands.add_parser("lights", help="make keys glow with how far they are pressed")
    l.add_argument("--keys", help="only these keys respond, like WASD")
    l.add_argument("--off", action="store_true", help="go back to your normal lighting, if something quit without restoring it")
    _add_light_options(l)
    l.add_argument("--hz", type=float, help="updates per second")
    l.set_defaults(func=cmd_lights)

    ac = commands.add_parser("actuation", help="see or set where keys actuate, no GG needed")
    ac.add_argument("keys", nargs="?", help="W, WASD, W,A,SPACE, or all. Leave out to just see the current settings")
    ac.add_argument("mm", nargs="?", type=float, help="depth from 0.1 to 4.0; uses the nearest of the firmware's 20 levels")
    ac.set_defaults(func=cmd_actuation)

    tp = commands.add_parser("typeprint", help="experiment: recognise you by how you press keys. Not security")
    tp.add_argument("action", choices=("enroll", "check", "forget"), help="enroll: type a set phrase 8 times. check: try to pass. forget: delete it")
    tp.set_defaults(func=cmd_typeprint)

    pf = commands.add_parser("profile", help="learn how you type, as per-key totals only, and what that means for actuation")
    pf.add_argument("--minutes", type=float, help="stop after this long (default: until Ctrl-C)")
    pf.add_argument("--report", action="store_true", help="show what's recorded without recording more")
    pf.add_argument("--reset", action="store_true", help="delete what's recorded")
    pf.add_argument("--hz", type=float, help="updates per second")
    pf.set_defaults(func=cmd_profile)

    h = commands.add_parser("health", help="check every switch for drift, noise and short range")
    h.add_argument("--press", type=float, metavar="SEC", help="also watch you press each key and report any that don't reach the bottom")
    h.add_argument("--all", action="store_true", help="list every key")
    h.set_defaults(func=cmd_health)

    o = commands.add_parser("overlay", help="show key pressure in OBS or any browser")
    o.add_argument("--port", type=int, default=8765)
    o.add_argument("--keys", default="W,A,S,D,SPACE,LSHIFT,LCTRL", help="keys to show (default W,A,S,D,SPACE,LSHIFT,LCTRL). 'all' shows your typing to viewers")
    o.add_argument("--hz", type=float, help="updates per second")
    o.set_defaults(func=cmd_overlay)

    i = commands.add_parser("info", help="the keyboard, your access, and every key's actuation settings")
    i.add_argument("--json", action="store_true")
    i.set_defaults(func=cmd_info)
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    try:
        return args.func(args) or 0
    except KeyboardInterrupt:
        return 0
    except BrokenPipeError:
        os.dup2(os.open(os.devnull, os.O_WRONLY), 1)  # the reader left, as with | head
        return 0
    except (ApexError, ValueError) as e:
        _say(f"apex-analog: {e}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
