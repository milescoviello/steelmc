#!/usr/bin/env python3
"""apex-analog: how far every key is pressed on a SteelSeries Apex Pro Gen 3, on Linux, wired to anything.

  apex-analog                   live bars for whatever you press
  apex-analog gamepad           W S A D become throttle, brake and steering on a virtual controller
  apex-analog map W=rt A=lx-    any key to any controller axis or button, the mouse, or a keystroke
  apex-analog stream            one JSON line per update, for piping into anything
  apex-analog info              the keyboard, your access, and every key's actuation settings

  from apex_analog import Keyboard
  with Keyboard() as kb:
      print(kb.read()["W"])     # millimetres

Only ever sends the read commands in ALLOWED, and only to the firmware they were checked on. No dependencies.
"""
import argparse
import base64
import fcntl
import glob
import hashlib
import json
import os
import select
import signal
import socket
import struct
import sys
import termios
import threading
import time
from typing import NamedTuple

__version__ = "0.1.0"

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

# The firmware's own actuation levels: position as n/255 of a key's calibrated range -> distance in mm.
# The sensors are not linear: half the sensor range is already 3.0 mm.
CURVE = [(0, 0.0), (5, 0.2), (8, 0.4), (12, 0.6), (16, 0.8), (21, 1.0), (26, 1.2), (31, 1.4), (38, 1.6), (46, 1.8), (55, 2.0),
         (65, 2.2), (77, 2.4), (91, 2.6), (108, 2.8), (128, 3.0), (151, 3.2), (180, 3.4), (206, 3.7), (215, 4.0)]
TRAVEL_MM = 4.0
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
        if "@" in target:
            target, at_text = target.rsplit("@", 1)
            at = float(at_text)
        self.sign = 1
        self.digital = None
        if target.startswith("key:"):
            self.kind, self.target = "key", keycode(target[4:])
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
                             "Mouse: mouse-x- mouse-x+ mouse-y- mouse-y+ wheel- wheel+ hwheel- hwheel+ mouse-left mouse-right mouse-middle. "
                             "Keystroke: key:F13")
        if self.kind in ("key", "mouse-button", "button"):
            self.digital = Digital(at, rapid)


def expand_maps(items):
    out = []
    for item in items:
        out += PRESETS[item].split() if item in PRESETS else [item]
    return out


class Mapper:
    """Turns key depths into controller, mouse and keystroke output. Nothing is created until it is needed."""

    def __init__(self, maps, deadzone=0.3, full=3.5, curve=1.0, rapid=0.0, mouse_speed=1200.0, wheel_speed=15.0, swallow=True, pause_key="PAUSE"):
        if not full > deadzone:
            raise ValueError("--full must be deeper than --deadzone")
        self.maps = [Mapping(m, rapid) for m in expand_maps(maps)]
        if not self.maps:
            raise ValueError("nothing to map. Try: apex-analog map drive")
        self.deadzone, self.full, self.curve = deadzone, full, curve
        self.mouse_speed, self.wheel_speed = mouse_speed, wheel_speed
        self.want_swallow, self.pause_code = swallow, keycode(pause_key) if pause_key else None
        self.keys = sorted({m.key for m in self.maps}, key=lambda n: BY_NAME[n][0])
        kinds = {m.kind for m in self.maps}
        self.pad = Gamepad() if kinds & {"stick", "trigger", "button"} else None
        self.mouse = Mouse() if kinds & {"mouse-axis", "mouse-button"} else None
        self.virtual_keys, self.swallow = None, None
        self.key_outputs = {m.target for m in self.maps if m.kind == "key"}
        self.remainder = {}
        self.values = {}
        self.warned = False

    @property
    def paused(self):
        return bool(self.swallow and self.swallow.paused)

    def shape(self, mm):
        x = (mm - self.deadzone) / (self.full - self.deadzone)
        return max(0.0, min(1.0, x)) ** self.curve

    def attach(self):
        """Hold back the mapped keys and pass the rest through. Without access this still maps; the keys also keep typing."""
        blocked = {BY_NAME[m.key][2] for m in self.maps if BY_NAME[m.key][2] is not None}
        swallow = None
        if self.want_swallow:
            try:
                swallow = Swallow(blocked, None, self.pause_code)
            except OSError as e:
                self._warn(f"Mapped keys will keep typing: no access to the keyboard's input devices ({e}). Run ./install.sh once.")
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
                self._warn("Mapped keys will keep typing: a key stayed held down, so the keyboard was not taken over.")

    def _warn(self, message):
        if not self.warned:
            _say(message)
            self.warned = True

    def update(self, depths, dt):
        if self.paused:
            depths = {}
        sticks, triggers, digital, keys, mice = {}, {}, {}, {}, {}
        for m in self.maps:
            mm = depths.get(m.key, 0.0)
            if m.kind == "stick":
                sticks[m.target] = sticks.get(m.target, 0.0) + m.sign * self.shape(mm)
            elif m.kind == "trigger":
                triggers[m.target] = max(triggers.get(m.target, 0.0), self.shape(mm))
            elif m.kind == "mouse-axis":
                rate = self.wheel_speed if "wheel" in m.target else self.mouse_speed
                mice[m.target] = mice.get(m.target, 0.0) + m.sign * self.shape(mm) * rate * dt
            else:
                state = m.digital.update(mm)
                table = {"key": keys, "button": digital, "mouse-button": mice}[m.kind]
                slot = ("button", m.target) if m.kind == "mouse-button" else m.target
                table[slot] = table.get(slot, False) or state
        if self.pad:
            self.pad.set(**sticks, **triggers, **digital)
        self.values = {**sticks, **triggers, **{k: v for k, v in digital.items()}}
        if self.mouse:
            whole = {}
            for axis, amount in mice.items():
                if isinstance(axis, tuple):
                    if self.remainder.get(axis) != amount:
                        self.remainder[axis] = amount
                        self.mouse.button(axis[1], amount)
                    continue
                total = self.remainder.get(axis, 0.0) + amount
                whole[axis] = int(total)
                self.remainder[axis] = total - whole[axis]
            self.mouse._rel(**whole)
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
        if self.swallow:
            self.swallow.stop()
            self.swallow = None

    def close(self):
        self.release()
        for device in (self.pad, self.mouse, self.virtual_keys):
            if device:
                device.close()


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
                body = json.dumps(self.latest).encode()
                client.sendall(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nConnection: close\r\nContent-Length: "
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


def _loop(hz, keys, mapper, on_frame):
    """Read, map, send. Comes back after an unplug or a suspend, and lets go of every output while the keyboard is away."""
    Keyboard().close()  # a problem at the start should be reported, not waited on
    waiting = False
    while True:
        try:
            with Keyboard() as kb:
                if waiting:
                    _say("Keyboard is back.")
                    waiting = False
                if mapper:
                    missing = [k for k in mapper.keys if k not in kb.keys]
                    if missing:
                        raise ApexError(f"No sensor fitted on this keyboard for: {' '.join(missing)}")
                    mapper.attach()
                last = time.monotonic()
                for depths in kb.stream(hz, keys):
                    now = time.monotonic()
                    if mapper:
                        mapper.update(depths, now - last)
                    last = now
                    on_frame(depths, now)
        except (Disconnected, NotFound, AccessDenied) as e:
            if isinstance(e, AccessDenied) and not waiting:
                raise
            if mapper:
                mapper.release()
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


def _map_lines(mapper, rate):
    lines = [f"apex-analog  {rate:.0f} updates/s  " + " ".join(m.text for m in mapper.maps), ""]
    for name, value in mapper.values.items():
        if name in Gamepad.STICKS:
            half = 20
            left = int(half * max(0.0, -value))
            right = int(half * max(0.0, value))
            lines.append(f"{name:7}|{' ' * (half - left)}{'#' * left}|{'#' * right}{' ' * (half - right)}| {value:+.2f}")
        elif name in Gamepad.TRIGGERS:
            n = int(41 * value)
            lines.append(f"{name:7}|{'#' * n}{' ' * (41 - n)}| {value:.2f}")
        else:
            lines.append(f"{name:7}{'ON' if value else 'off'}")
    if mapper.mouse:
        lines.append("mouse is live")
    if mapper.key_outputs:
        lines.append("keystrokes are live")
    lines.append("")
    if mapper.paused:
        lines.append("PAUSED: your keys type normally. Press Pause to resume.")
    elif mapper.swallow:
        lines.append("Mapped keys won't type while this runs. Pause key pauses. Ctrl-C quits.")
    else:
        lines.append("Ctrl-C quits.")
    return lines


def cmd_map(args):
    sinks = _network_sinks(args)
    explicit = _selected_keys(args)
    maps = args.maps or (["drive"] if args.command == "gamepad" else [])
    Keyboard().close()  # say what's wrong with the keyboard before creating virtual devices
    mapper = Mapper(maps, args.deadzone, args.full, args.curve, args.rapid, args.mouse_speed, args.wheel_speed,
                    not args.no_swallow, args.pause_key)
    read = sorted({*mapper.keys, *(explicit or [])}, key=lambda n: BY_NAME[n][0]) if args.keys != "all" else None
    state = {"scans": 0, "rate": 0.0, "last": time.monotonic(), "drawn": 0.0}
    try:
        if mapper.pad:
            _say(f"Virtual controller ready: {mapper.pad.path}")
        with _Screen() as screen:
            def on_frame(depths, now):
                for sink in sinks:
                    sink.send(_frame_message({k: v for k, v in depths.items() if not explicit or k in explicit}, explicit))
                state["scans"] += 1
                if now - state["last"] >= 1:
                    state.update(rate=state["scans"] / (now - state["last"]), scans=0, last=now)
                if now - state["drawn"] >= 0.05:
                    state["drawn"] = now
                    screen.draw(_map_lines(mapper, state["rate"]))
            _loop(args.hz or 120, read, mapper, on_frame)
    finally:
        mapper.close()


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
                          ("map", "send keys to controller axes and buttons, the mouse, or keystrokes")):
        p = commands.add_parser(name, help=summary, fromfile_prefix_chars="@", description=summary, epilog=(
            "Rules look like KEY=OUTPUT, or KEY=OUTPUT@mm for on/off outputs. Sticks: lx- lx+ ly- ly+ rx- rx+ ry- ry+. Triggers: lt rt. "
            "Buttons: a b x y lb rb back start guide ls rs dpad-up dpad-down dpad-left dpad-right. "
            "Mouse: mouse-x- mouse-x+ mouse-y- mouse-y+ wheel- wheel+ hwheel- hwheel+ mouse-left mouse-right mouse-middle. "
            f"Keystroke: key:F13. Presets: {', '.join(PRESETS)}. Keep rules in a file, one per line, and pass @file."))
        p.add_argument("maps", nargs="*", metavar="RULE", help="W=rt, A=lx-, J=key:F13@2.0, or a preset")
        p.add_argument("--deadzone", type=float, default=0.3, metavar="MM", help="axis stays 0 above this depth (default 0.3)")
        p.add_argument("--full", type=float, default=3.5, metavar="MM", help="axis reaches 1 at this depth (default 3.5)")
        p.add_argument("--curve", type=float, default=1.0, help="above 1 gives finer control near the top of the press (default 1, try 1.5)")
        p.add_argument("--rapid", type=float, default=0.0, metavar="MM", help="on/off outputs re-trigger after moving this far either way")
        p.add_argument("--mouse-speed", type=float, default=1200.0, metavar="PX/S", help="cursor speed at full depth (default 1200)")
        p.add_argument("--wheel-speed", type=float, default=15.0, metavar="N/S", help="scroll speed at full depth (default 15)")
        p.add_argument("--no-swallow", action="store_true", help="let mapped keys keep typing as well")
        p.add_argument("--pause-key", default="PAUSE", metavar="KEY", help="key that pauses mapping so you can type (default PAUSE)")
        _add_network_options(p)
        p.set_defaults(func=cmd_map)

    s = commands.add_parser("stream", help="one JSON line per update, or send it over UDP, OSC, or WebSocket")
    s.add_argument("--events", action="store_true", help="send key presses and releases with their speed instead of depths")
    s.add_argument("--down", type=float, default=0.5, metavar="MM", help="--events: counts as pressed at this depth (default 0.5)")
    s.add_argument("--up", type=float, default=0.3, metavar="MM", help="--events: counts as released at this depth (default 0.3)")
    _add_network_options(s)
    s.set_defaults(func=cmd_stream)

    i = commands.add_parser("info", help="the keyboard, your access, and every key's actuation settings")
    i.add_argument("--json", action="store_true")
    i.set_defaults(func=cmd_info)
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    try:
        args.func(args)
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
