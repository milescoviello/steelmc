#!/usr/bin/env python3
"""apex-analog: how far every key is pressed on a SteelSeries Apex Pro Gen 3, on Linux, wired to anything.

  apex-analog                   live bars for whatever you press
  apex-analog gamepad           W S A D become throttle, brake and steering on a virtual controller
  apex-analog map W=rt A=lx-    any key to a controller, pedals, the mouse, a keystroke, or a command
  apex-analog midi              play it as a MIDI instrument, with velocity and per-key pressure
  apex-analog type              press deep for capitals or key combos
  apex-analog lights            keys glow with depth; effects: ripple, music, stats, rev, heatmap
  apex-analog paint             set key colours from any program, one JSON line at a time
  apex-analog stream            one JSON line per update, for piping into anything
  apex-analog overlay           key pressure on your stream, as an OBS browser source
  apex-analog profile           how you type, and what it means for your actuation point
  apex-analog status            what's on. apex-analog off NAME turns one off, apex-analog stop turns off everything
  apex-analog help              everything it can do
  apex-analog health            check every switch for drift, noise and short range
  apex-analog typeprint         experiment: recognise you by how you press keys
  apex-analog actuation W 1.0   set where keys actuate, no GG needed
  apex-analog rapid WASD on     turn the keyboard's rapid tap on or off per key
  apex-analog bind CAPS esc     change what a key does, on the keyboard itself, so it works anywhere
  apex-analog preset save NAME  keep setups on this computer, then preset use NAME
  apex-analog save              write the current settings to the keyboard's flash
  apex-analog auto              switch presets and lighting by the focused window
  apex-analog macro add NAME    save a sequence of keystrokes, then fire it with J=macro:NAME
  apex-analog oled              show key depth on the keyboard's own screen
  apex-analog magnet            watch the sensors react to a magnet
  apex-analog info              the keyboard, your access, and every key's actuation settings

  from apex_analog import Keyboard
  with Keyboard() as kb:
      print(kb.read()["W"])     # millimetres

A small daemon owns the keyboard and every command goes through it, so long-running ones return your terminal straight away
and any number can run at once. Only sends the read commands in ALLOWED, plus LIGHT_COMMANDS when you ask for lights, SETTING_COMMANDS when you set
actuation or rapid tap, and BIND_COMMANDS when you rebind a key, and only to the firmware they were checked on. The one flash write, SAVE_COMMAND, runs only from
`apex-analog save` after you confirm it. No dependencies.
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
import shutil
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
    0xE2: "which saved config is active",
    0xE0: "which key turns on the SteelSeries layer while it's held",
    0xE3: "what up to 10 keys do, on one layer",
    0xE4: "what the volume wheel does, on one layer",
    0xB3: "each key's press and release points, or with 2 first, where its deeper press starts",
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
    0x32: "set the mode byte of up to 30 keys",
    0x33: "with 2 first, set where the deeper press of up to 20 keys starts",
}
DEEP_OFF = 255  # a second actuation point at the very bottom, which is how every key starts out
RAPID_TAP_MODE = 2  # the value the keyboard's own RAPID TAP menu sets
# Change what keys do, on the keyboard itself, only when you ask. Each handler stores in memory and reaches no flash save,
# so a replug undoes them unless you save. Every message is checked against BIND_CHECKS before it's sent.
BIND_COMMANDS = {
    0x63: "set what up to 10 keys do, on one layer",
    0x64: "set what the volume wheel does, on one layer",
    0x60: "choose which key turns on the SteelSeries layer while held",
}
# The one command that writes flash: all current settings, over one of the five saved configs. Same code as the Save Config menu.
SAVE_COMMAND = 0x09

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
# Keys with an LED but no sensor, by HID usage.
NO_SENSOR_HIDS = {"ESC": 0x29, **{f"F{i}": 0x39 + i for i in range(1, 13)}, "PRTSC": 0x46, "SCRLK": 0x47, "PAUSE": 0x48, "INSERT": 0x49, "HOME": 0x4A,
                  "PGUP": 0x4B, "DELETE": 0x4C, "END": 0x4D, "PGDN": 0x4E, "RIGHT": 0x4F, "LEFT": 0x50, "DOWN": 0x51, "UP": 0x52, "MENU": 0x65}

# ---- key bindings on the keyboard itself ------------------------------------------------------------------------------
# Every key the firmware's own table lets you rebind, by name. Keys with a sensor can also take a second action on a deeper
# press. The numpad and the controls around the wheel have no sensor. The wheel itself is bound separately, with 64.
NUMPAD_HIDS = {"KP_NUM": 0x53, "KP_DIV": 0x54, "KP_MUL": 0x55, "KP_SUB": 0x56, "KP_ADD": 0x57, "KP_ENTER": 0x58,
               **{f"KP{n}": 0x58 + n for n in range(1, 10)}, "KP0": 0x62, "KP_DOT": 0x63, "KP_COMMA": 0x85}
WHEEL_KEY_HIDS = {"WHEEL_PRESS": 0xF1, "MEDIA_BUTTON": 0xF5, "TILT_RIGHT": 0xF6, "TILT_LEFT": 0xF7}
BIND_HIDS = {**{name: hid for name, (_, hid, _) in BY_NAME.items()},
             **{name: hid for name, hid in NO_SENSOR_HIDS.items() if name != "MENU"}, **NUMPAD_HIDS, **WHEEL_KEY_HIDS}
BIND_NAMES = {hid: name for name, hid in BIND_HIDS.items()}
DEEP_HIDS = {hid for _, hid, _ in BY_NAME.values()}
LAYERS = {"base": 0, "ss": 1, "deep": 2}
LAYER_NAMES = {code: name for name, code in LAYERS.items()}
LAYER_LABELS = {0: "normally", 1: "while the SteelSeries key is held", 2: "on a deeper press"}
WHEEL_DIRECTIONS = ("WHEEL_UP", "WHEEL_DOWN")

# What a key can send: keyboard usages by name, spelled as aerox spells them so both tools take the same actions.
OUTPUT_KEYS = {0x04 + i: chr(ord("a") + i) for i in range(26)}
OUTPUT_KEYS.update({0x1E + i: str((i + 1) % 10) for i in range(10)})
OUTPUT_KEYS.update({0x3A + i: f"f{i + 1}" for i in range(12)})
OUTPUT_KEYS.update({0x68 + i: f"f{i + 13}" for i in range(12)})
OUTPUT_KEYS.update(zip(range(0x28, 0x39), ("enter", "esc", "backspace", "tab", "space", "-", "=", "[", "]", "\\", "#", ";", "'", "`",
                                           ",", ".", "/")))
OUTPUT_KEYS.update(zip(range(0x46, 0x53), ("printscreen", "scrolllock", "pause", "insert", "home", "pageup", "delete", "end",
                                           "pagedown", "right", "left", "down", "up")))
OUTPUT_KEYS[0x39] = "capslock"
OUTPUT_KEYS.update(zip(range(0xE0, 0xE8), ("ctrl", "shift", "alt", "meta", "rctrl", "rshift", "ralt", "rmeta")))
OUTPUT_KEYS.update({0x53: "numlock", 0x54: "kp/", 0x55: "kp*", 0x56: "kp-", 0x57: "kp+", 0x58: "kp-enter",
                    **{0x58 + n: f"kp{n}" for n in range(1, 10)}, 0x62: "kp0", 0x63: "kp.", 0x85: "kp,", 0x64: "iso\\", 0x65: "menu",
                    **{0x86 + n: f"int{n}" for n in range(1, 6)}})
OUTPUT_ALIASES = {"control": "ctrl", "win": "meta", "super": "meta", "return": "enter", "escape": "esc", "del": "delete",
                  "pgup": "pageup", "pgdn": "pagedown", "prtsc": "printscreen"}
OUTPUT_USAGES = {name: usage for usage, name in OUTPUT_KEYS.items()}
# apex-analog's own key names work too: LCTRL, LGUI, BKSP, CAPS, KP_ENTER.
for _name, _hid in BIND_HIDS.items():
    if _hid in OUTPUT_KEYS and _name.lower() not in OUTPUT_USAGES:
        OUTPUT_ALIASES.setdefault(_name.lower(), OUTPUT_KEYS[_hid])
del _name, _hid
MEDIA_USAGES = {0xB5: "next", 0xB6: "previous", 0xB7: "stop", 0xCD: "play-pause", 0xE2: "mute", 0xE9: "volume-up", 0xEA: "volume-down"}
MEDIA_NAMES = {name: code for code, name in MEDIA_USAGES.items()}
SIMPLE_BINDINGS = {"none": 0x00, "scroll-up": 0x31, "scroll-down": 0x32, "tilt-left": 0x33, "tilt-right": 0x34}
SIMPLE_NAMES = {code: name for name, code in SIMPLE_BINDINGS.items()}
KEYS_KIND, MEDIA_KIND, BUILTIN_KIND = 0x51, 0x61, 0x62
# The keyboard's own functions, as its defaults put them on the SteelSeries layer: (function, when) -> the key they start on.
# "When" is 0 on press, 1 while held. Only these can be sent, so they can be moved or put back, never invented.
BUILTINS = {(1, 0): "F9", (2, 1): "F10", (3, 1): "F11", (4, 1): "F12", (5, 0): "LGUI", (6, 0): "O", (7, 0): "I", (8, 0): "T",
            (10, 0): "Q"}


def default_binding(layer, hid):
    """What a key does out of the box on each layer, from the firmware's restore-defaults code."""
    if layer == 0:
        special = {0xF1: (MEDIA_KIND, 0xE2), 0xF5: (MEDIA_KIND, 0xCD), 0xF6: (MEDIA_KIND, 0xB5), 0xF7: (MEDIA_KIND, 0xB6)}
        return bytes(special.get(hid, (KEYS_KIND, hid))).ljust(5, b"\0")
    if layer == 1:
        for (function, when), name in BUILTINS.items():
            if BIND_HIDS[name] == hid:
                return bytes([BUILTIN_KIND, function, when, 0, 0])
    return bytes(5)


DEFAULT_WHEEL = {0: (bytes([MEDIA_KIND, 0xE9, 0, 0, 0]), bytes([MEDIA_KIND, 0xEA, 0, 0, 0])), 1: (bytes(5), bytes(5))}


def describe_binding(record):
    """A binding as the text parse_binding takes, so what's printed can be typed back in."""
    record = bytes(record)
    kind = record[0]
    if 0x01 <= kind <= 0x08 and not any(record[1:]):
        return f"mouse{kind}"
    if kind in SIMPLE_NAMES and not any(record[1:]):
        return SIMPLE_NAMES[kind]
    if kind == KEYS_KIND:
        keys = [OUTPUT_KEYS.get(k, f"{k:#04x}") for k in record[1:] if k]
        return "+".join(keys) if keys else "none"
    if kind == MEDIA_KIND and not any(record[2:]):
        return "media:" + MEDIA_USAGES.get(record[1], f"{record[1]:#04x}")
    if kind == BUILTIN_KIND and (record[1], record[2]) in BUILTINS and not any(record[3:]):
        return f"builtin:{record[1]}" + (f":{record[2]}" if record[2] else "")
    return "raw:" + record.hex()


def parse_binding(text):
    """Turn `esc`, `ctrl+c`, `media:mute`, `mouse4`, `scroll-up`, `builtin:10` or `none` into the keyboard's 5-byte record."""
    t = text.strip().lower()
    if t == "nothing":
        t = "none"
    if t in SIMPLE_BINDINGS:
        return bytes([SIMPLE_BINDINGS[t], 0, 0, 0, 0])
    match = re.fullmatch(r"mouse ?([1-8])", t)
    if match:
        return bytes([int(match.group(1)), 0, 0, 0, 0])
    if t.startswith("media:"):
        name = t[6:]
        if name not in MEDIA_NAMES:
            raise ValueError(f"Unknown media action {name!r}. Choose from: {', '.join(MEDIA_NAMES)}.")
        return bytes([MEDIA_KIND, MEDIA_NAMES[name], 0, 0, 0])
    match = re.fullmatch(r"builtin:(\d+)(?::(\d+))?", t)
    if match:
        key = (int(match.group(1)), int(match.group(2) or 0))
        if key not in BUILTINS:
            raise ValueError("The keyboard's own functions can only be moved, not made up. Known: "
                             + ", ".join(f"builtin:{f}" + (f":{w}" if w else "") + f" (normally SS+{k})" for (f, w), k in BUILTINS.items()))
        return bytes([BUILTIN_KIND, key[0], key[1], 0, 0])
    if t.startswith("keys:"):
        t = t[5:]
    names = [OUTPUT_ALIASES.get(n.strip(), n.strip()) for n in re.split(r"\+(?!$)", t)]
    if not 1 <= len(names) <= 4:
        raise ValueError("A key can send up to 4 keys at once.")
    unknown = [n for n in names if n not in OUTPUT_USAGES]
    if unknown:
        raise ValueError(f"Unknown key or action: {', '.join(unknown)}.")
    return bytes([KEYS_KIND, *(OUTPUT_USAGES[n] for n in names)]).ljust(5, b"\0")


def _check_record(record):
    """Only records whose effect was read in the firmware's key handling, with nothing stray in the unused bytes."""
    record = bytes(record)
    if len(record) != 5:
        raise ValueError(f"a binding is 5 bytes, not {len(record)}")
    kind, rest = record[0], record[1:]
    if kind == 0x00 or 0x01 <= kind <= 0x08 or kind in SIMPLE_NAMES:
        ok = not any(rest)
    elif kind == KEYS_KIND:
        ok = any(rest) and all(k == 0 or k in OUTPUT_KEYS for k in rest)
    elif kind == MEDIA_KIND:
        ok = rest[0] in MEDIA_USAGES and not any(rest[1:])
    elif kind == BUILTIN_KIND:
        ok = (rest[0], rest[1]) in BUILTINS and not any(rest[2:])
    else:
        ok = False
    if not ok:
        raise ValueError(f"refused to send binding {record.hex(' ')}: not a kind this tool knows is safe")


def _check_bind(payload):
    """63 <layer> <n> [<hid> <record>]... The firmware writes the normal layer for any layer byte but 1 or 2, so only 0-2 pass."""
    if len(payload) < 3 or payload[1] not in LAYER_NAMES:
        raise ValueError("refused to send 63: the layer must be 0, 1 or 2")
    count = payload[2]
    if not 1 <= count <= 10 or len(payload) != 3 + 6 * count:
        raise ValueError("refused to send 63: it takes 1 to 10 keys, 6 bytes each")
    for i in range(count):
        hid, record = payload[3 + 6 * i], payload[4 + 6 * i:9 + 6 * i]
        if hid not in BIND_NAMES:
            raise ValueError(f"refused to send 63: {hid:#04x} isn't a key that can be bound")
        if payload[1] == 2 and hid not in DEEP_HIDS:
            raise ValueError(f"refused to send 63: {BIND_NAMES[hid]} has no sensor, so no deeper press")
        if bytes(record) != default_binding(payload[1], hid):  # putting a key back is always allowed
            _check_record(record)


def _check_wheel(payload):
    """64 <layer> <up record> <down record>. Any layer byte but 0 writes the SteelSeries layer, so only 0 and 1 pass."""
    if len(payload) != 12 or payload[1] not in (0, 1):
        raise ValueError("refused to send 64: it takes layer 0 or 1, then two 5-byte bindings")
    for record, default in zip((payload[2:7], payload[7:12]), DEFAULT_WHEEL[payload[1]]):
        if bytes(record) != default:
            _check_record(record)


def _check_layer_key(payload):
    """60 <hid>. Only a key the firmware can bind, so the layer always stays reachable from a real key."""
    if len(payload) != 2 or payload[1] not in BIND_NAMES:
        raise ValueError("refused to send 60: it takes one key the keyboard can bind")


BIND_CHECKS = {0x63: _check_bind, 0x64: _check_wheel, 0x60: _check_layer_key}


def _check_deep_actuation(payload):
    """33 2 <n> [<hid> <press> <release>]... Only the second actuation point: 31 already sets the first, and 33's other forms
    set it too, with different checks."""
    if len(payload) < 3 or payload[1] != 2:
        raise ValueError("refused to send 33: only its second actuation form, 33 02, is used")
    count = payload[2]
    if not 1 <= count <= 20 or len(payload) != 3 + 3 * count:
        raise ValueError("refused to send 33: it takes 1 to 20 keys, 3 bytes each")
    for i in range(count):
        hid, press, release = payload[3 + 3 * i:6 + 3 * i]
        if hid not in DEEP_HIDS or hid == BY_NAME["LGUI"][1]:
            raise ValueError(f"refused to send 33: {hid:#04x} can't have a deeper press")
        if not 1 <= release <= press:
            raise ValueError("refused to send 33: release must be from 1 up to the press point")


SETTING_CHECKS = {0x33: _check_deep_actuation}


def bind_key(key):
    """Accept caps, CAPS, f13... any key the keyboard can rebind, or WHEEL_UP and WHEEL_DOWN."""
    name = key.upper() if len(key) > 1 or key.isalpha() else key
    name = ALIASES.get(name, name)
    if name in BIND_HIDS or name in WHEEL_DIRECTIONS:
        return name
    raise ValueError(f"{key!r} isn't a key the keyboard can rebind. Keys: " + " ".join(sorted(k for k in BIND_HIDS if not k.startswith("INT")))
                     + " " + " ".join(WHEEL_DIRECTIONS))


def _key_positions():
    """Where each key sits on an ANSI board, in key widths, for effects that move across the keyboard."""
    out = {}

    def place(y, x, names):
        for i, name in enumerate(names):
            out[name] = (x + i, y)
    place(0, 0, ["ESC"])
    place(0, 2, ["F1", "F2", "F3", "F4"])
    place(0, 6.5, ["F5", "F6", "F7", "F8"])
    place(0, 11, ["F9", "F10", "F11", "F12"])
    place(0, 15.25, ["PRTSC", "SCRLK", "PAUSE"])
    place(1.25, 0, list("`1234567890-="))
    out["BKSP"] = (13.5, 1.25)
    place(1.25, 15.25, ["INSERT", "HOME", "PGUP"])
    out["TAB"] = (0.25, 2.25)
    place(2.25, 1.5, list("QWERTYUIOP[]"))
    out["\\"] = (13.75, 2.25)
    place(2.25, 15.25, ["DELETE", "END", "PGDN"])
    out["CAPS"] = (0.4, 3.25)
    place(3.25, 1.75, list("ASDFGHJKL;'"))
    out["ENTER"] = (13.4, 3.25)
    out["LSHIFT"] = (0.6, 4.25)
    place(4.25, 2.25, list("ZXCVBNM,./"))
    out["RSHIFT"] = (13.4, 4.25)
    out["UP"] = (16.25, 4.25)
    out.update({"LCTRL": (0.1, 5.25), "LGUI": (1.35, 5.25), "LALT": (2.6, 5.25), "SPACE": (6.5, 5.25), "RALT": (10.1, 5.25), "RGUI": (11.35, 5.25),
                "SS": (12.6, 5.25), "RCTRL": (13.85, 5.25), "LEFT": (15.25, 5.25), "DOWN": (16.25, 5.25), "RIGHT": (17.25, 5.25)})
    return out


POSITIONS = _key_positions()
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


def led_hid(key):
    """The HID usage of any key with an LED, sensor or not."""
    name = key.upper() if len(key) > 1 or key.isalpha() else key
    name = ALIASES.get(name, name)
    if name in BY_NAME:
        return BY_NAME[name][1]
    if name in NO_SENSOR_HIDS:
        return NO_SENSOR_HIDS[name]
    raise ValueError(f"{key!r} isn't a key with an LED. Known: " + " ".join(sorted(k for k in {*BY_NAME, *NO_SENSOR_HIDS} if not k.startswith("INT"))))


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

    client = None
    fd = None

    def __init__(self, direct=False):
        """Goes through the daemon if one is running. direct=True talks to the keyboard itself, which is what the daemon does."""
        if not direct:
            try:
                client = DaemonClient()
            except OSError:
                if _in_job():
                    raise NotFound("The apex-analog daemon isn't running.") from None
            else:
                try:
                    info = json.loads(client.call(OP_OPEN))
                except ApexError:
                    client.close()
                    raise
                self.client, self.node, self.firmware = client, info["node"], info["firmware"]
                self._load_calibration()
                return
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
            raise AccessDenied("Another program is using the keyboard directly. Replies carry no ID, so only one can.") from None
        self._load_calibration()

    def _load_calibration(self):
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
        self.channel_names = {BY_NAME[n][0]: n for n in self.keys}

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def close(self):
        if self.client is not None:
            self.client.close()
            self.client = None
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None

    def exchange_once(self, payload):
        """One command and whatever comes back first, for the daemon. Empty if nothing did."""
        try:
            while select.select([self.fd], [], [], 0)[0]:
                os.read(self.fd, 64)
            os.write(self.fd, bytes([0, *payload]).ljust(65, b"\0")[:65])
            if select.select([self.fd], [], [], 0.03)[0]:
                return os.read(self.fd, 64)
        except OSError as e:
            raise Disconnected(f"Lost the keyboard: {e}") from None
        return b""

    def _ask(self, payload, valid=None):
        if not payload or payload[0] not in ALLOWED:
            raise ValueError(f"refused to send {payload[:1]!r}: not a known read command")
        if self.client is not None:
            for _ in range(10):
                reply = self.client.call(OP_ASK, bytes(payload))
                if reply and (valid is None or valid(reply)):
                    return reply
            raise Disconnected("The keyboard stopped answering.")
        packet = bytes([0, *payload]).ljust(65, b"\0")[:65]
        try:
            for _ in range(10):  # a reply normally takes 1 ms, but one can get lost right after a write, so retry soon
                while select.select([self.fd], [], [], 0)[0]:  # replies carry no ID, so never leave an old one waiting
                    os.read(self.fd, 64)
                os.write(self.fd, packet)
                deadline = time.monotonic() + 0.03
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
        """Yields read() forever at up to hz. All keys tops out near 190 Hz; two rows of keys near 490 Hz.
        keys can also be a function, asked before each read, for a mode that sometimes needs more keys than others."""
        dynamic = callable(keys)
        names = None if dynamic else key_names(keys)
        period, due = 1.0 / hz, time.monotonic()
        while True:
            yield self.read(key_names(keys()) if dynamic else names)
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
        level, press, release = actuation_level(mm)
        self.set_actuation_points(keys, press, release)
        return level

    def set_actuation_points(self, keys, press, release):
        """The same in the firmware's own units, n/255 of each key's range."""
        names = [n for n in key_names(keys) if n != "LGUI"]
        for start in range(0, len(names), 20):
            part = names[start:start + 20]
            self._write([0x31, len(part)] + [byte for name in part for byte in (BY_NAME[name][1], press, release)])

    def set_mode(self, keys, mode):
        """Set each key's mode byte. RAPID_TAP_MODE turns on the keyboard's own rapid tap, 0 turns it off."""
        names = key_names(keys)
        for start in range(0, len(names), 30):
            part = names[start:start + 30]
            self._write([0x32, len(part)] + [byte for name in part for byte in (BY_NAME[name][1], mode)])

    def deep_actuation(self, keys=None):
        """Where each key's deeper press starts: {'W': {'press': 3.0, 'release': 2.9, 'press_raw': 128, 'release_raw': 120}},
        or None for a key whose second point is still at the very bottom, as every key starts out."""
        names = key_names(keys) or self.keys
        out = {}
        for start in range(0, len(names), 20):
            part = names[start:start + 20]
            hids = [BY_NAME[n][1] for n in part]
            request = [0xB3, 2, len(part)] + [byte for hid in hids for byte in (hid, 0, 0)]
            reply = self._ask(request, lambda r, h=tuple(hids): len(r) >= 3 * len(h) and all(r[3 * i] == x for i, x in enumerate(h)))
            for i, name in enumerate(part):
                press, release = reply[3 * i + 1], reply[3 * i + 2]
                out[name] = None if press >= DEEP_OFF else {"press": round(curve_mm(press), 2), "release": round(curve_mm(release), 2),
                                                            "press_raw": press, "release_raw": release}
        return out

    def set_deep_actuation(self, keys, mm):
        """Set where these keys' deeper press starts, at the nearest firmware level, or None to put it back at the bottom.
        The keyboard ignores a point shallower than the key's first one, and LGUI can't have one."""
        if mm is None:
            press = release = DEEP_OFF
        else:
            _, press, release = actuation_level(mm)
        names = [n for n in key_names(keys) if n != "LGUI"]
        for start in range(0, len(names), 20):
            part = names[start:start + 20]
            self._write([0x33, 2, len(part)] + [byte for name in part for byte in (BY_NAME[name][1], press, release)])

    def active_slot(self):
        """Which of the five saved configs is active: the number on the OLED's home screen."""
        return self._ask([0xE2])[0]

    def save_to_keyboard(self, slot):
        """Write all current settings to flash over saved config 1 to 5. The firmware does no range check, so it happens here."""
        if not isinstance(slot, int) or not 1 <= slot <= 5:
            raise ValueError(f"config slot must be 1 to 5, not {slot!r}")
        self._write([SAVE_COMMAND, slot])
        deadline = time.monotonic() + 8.0
        while True:  # the keyboard is busy while it writes
            try:
                self._ask([0xE2])
                return
            except Disconnected:
                if time.monotonic() > deadline:
                    raise

    def bindings(self, layer=0, keys=None):
        """{'CAPS': b'\\x51\\x39\\0\\0\\0', ...}: what each key does on a layer. 0 is normal, 1 while the SteelSeries key is held,
        2 on a deeper press. describe_binding turns a record into text."""
        if layer not in LAYER_NAMES:
            raise ValueError(f"layer must be 0, 1 or 2, not {layer!r}")
        if keys is None:
            names = [n for n, hid in BIND_HIDS.items() if layer != 2 or hid in DEEP_HIDS]
        else:
            names = [bind_key(k) for k in keys]
            wheel = [n for n in names if n in WHEEL_DIRECTIONS]
            if wheel:
                raise ValueError("The wheel's bindings come from Keyboard.wheel().")
        if layer == 2:
            # The firmware's read goes out of step after a key without a sensor, so never ask about one on this layer.
            flat = [n for n in names if BIND_HIDS[n] not in DEEP_HIDS]
            if flat:
                raise ValueError(f"No sensor, so no deeper press: {' '.join(flat)}")
        out = {}
        for start in range(0, len(names), 10):
            part = names[start:start + 10]
            hids = [BIND_HIDS[n] for n in part]
            request = [0xE3, layer, len(part)] + [byte for hid in hids for byte in (hid, 0, 0, 0, 0, 0)]
            reply = self._ask(request, lambda r, h=tuple(hids): len(r) >= 6 * len(h) and all(r[6 * i] == x for i, x in enumerate(h)))
            for i, name in enumerate(part):
                out[name] = bytes(reply[6 * i + 1:6 * i + 6])
        return out

    def set_bindings(self, layer, changes):
        """Rebind keys on one layer: {'CAPS': parse_binding('esc')}. Stays in the keyboard's memory until save_to_keyboard."""
        items = [(BIND_HIDS[bind_key(name)], bytes(record)) for name, record in changes.items()]
        for start in range(0, len(items), 10):
            part = items[start:start + 10]
            self._write([0x63, layer, len(part)] + [byte for hid, record in part for byte in (hid, *record)])

    def wheel(self, layer=0):
        """(up, down): what turning the volume wheel does, normally (0) or while the SteelSeries key is held (1)."""
        if layer not in (0, 1):
            raise ValueError(f"the wheel has layer 0 or 1, not {layer!r}")
        reply = self._ask([0xE4, layer], lambda r: len(r) >= 10)
        return bytes(reply[:5]), bytes(reply[5:10])

    def set_wheel(self, layer, up, down):
        """Rebind the volume wheel. Stays in the keyboard's memory until save_to_keyboard."""
        self._write([0x64, layer, *bytes(up), *bytes(down)])

    def layer_key(self):
        """The HID usage of the key that turns on the SteelSeries layer while held. 0xF0, the SteelSeries key, out of the box."""
        return self._ask([0xE0])[0]

    def set_layer_key(self, key):
        """Move the SteelSeries layer to another key. Stays in the keyboard's memory until save_to_keyboard."""
        self._write([0x60, BIND_HIDS[bind_key(key)]])

    def channels(self):
        """Filtered sensor value of all 70 channels, including those with no switch fitted."""
        out = {}
        for bank in range(5):
            for i, value in enumerate(self._bank(bank + 1)):
                out[bank * BANK_SIZE + i] = value
        return out

    def _write(self, payload):
        """Send a command that gets no reply, then give the keyboard time to act on it. One that arrives straight after appears to replace it."""
        if not payload or (payload[0] not in LIGHT_COMMANDS and payload[0] not in SETTING_COMMANDS and payload[0] != SAVE_COMMAND
                           and payload[0] not in BIND_COMMANDS):
            raise ValueError(f"refused to send {payload[:1]!r}: not a known write command")
        check = BIND_CHECKS.get(payload[0]) or SETTING_CHECKS.get(payload[0])
        if check:
            check(payload)
        if self.client is not None:
            self.client.call(OP_WRITE, bytes(payload))  # the daemon leaves the gap, so nobody else's command lands in it
            return
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
        self._send_colors([[led_hid(key), *color] for key, color in colors.items()])

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


SHORTCUT_HOLD = "SS"  # hold the SteelSeries key and press another. The keyboard stops that key typing by itself
SHORTCUT_DOWN = 0.5  # mm
SS_TAP_SECONDS = 0.35  # a rule on SS itself fires on a quick tap with nothing else pressed, so holding it still works for combos
SHORTCUT_TAKEN = ("Q", "I", "O", "T", "LGUI")  # the keyboard's own combos, lit red while SS is held. F9-F12 and the arrows have no sensors


class Mapping:
    """One 'KEY=OUTPUT' or 'KEY=OUTPUT@mm' rule, or 'SS+KEY=OUTPUT' to need the SteelSeries key held.
    'SS=OUTPUT' fires on a quick tap of the SteelSeries key alone, since holding it is for its combos."""

    def __init__(self, text, rapid=0.0):
        self.chord = SHORTCUT_HOLD if text[:3].upper() == SHORTCUT_HOLD + "+" and len(text) > 3 else None
        self.ready = True
        whole, text = text, text[3:] if self.chord else text
        source, separator, target = text[1:].partition("=")  # text[0] may itself be the = key
        if not separator or not target:
            raise ValueError(f"{text!r} should look like W=rt or J=key:F13@2.0")
        source = text[0] + source
        self.text, self.key = whole, key_name(source)
        if self.chord and self.key in (*SHORTCUT_TAKEN, SHORTCUT_HOLD):
            raise ValueError(f"SS+{self.key} belongs to the keyboard. Hold SS to see the keys it uses, lit red.")
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
        if target.startswith("macro:") and target[6:]:
            self.kind, self.target = "macro", target[6:]
        elif target.startswith("key:"):
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
                             "Command: run:your-command. Macro: macro:NAME")
        if self.player == 2 and self.kind not in ("stick", "trigger", "button"):
            raise ValueError(f"{text!r}: p2: only applies to controller outputs")
        self.ss_tap = self.key == SHORTCUT_HOLD and not self.chord
        if self.ss_tap and self.kind not in ("key", "macro", "run"):
            raise ValueError(f"{text!r}: a tap of the SteelSeries key can send a keystroke, a macro or a command, not {target}")
        if self.kind in ("key", "mouse-button", "button", "macro") or (self.chord and self.kind == "run"):
            self.digital = Digital(at, rapid)


TEXT_SHIFTED = dict(zip('!@#$%^&*()_+{}|:"~<>?', "1234567890-=[]\\;'`,./"))
TEXT_NAMES = {" ": "SPACE", "\n": "ENTER", "\t": "TAB"}
MACRO_GAP_MS = 8  # between keystrokes, so apps see each one


def _text_events(text):
    """What to press to type this, on a US layout."""
    shift = keycode("LSHIFT")
    events = []
    for character in text:
        shifted = character.isupper() or character in TEXT_SHIFTED
        base = TEXT_SHIFTED.get(character, character)
        code = keycode(TEXT_NAMES.get(base, base))
        if shifted:
            events.append((shift, 1, MACRO_GAP_MS))
        events += [(code, 1, MACRO_GAP_MS), (code, 0, MACRO_GAP_MS)]
        if shifted:
            events.append((shift, 0, MACRO_GAP_MS))
    return events


def compile_macro(steps):
    """Saved steps -> (key code, down or up, ms to wait first). A step is a number of ms to wait, a combo like "LCTRL+C",
    {"text": "hello"}, or {"events": [[code, value, ms], ...]} from a recording."""
    events, wait = [], 0
    for step in steps:
        if isinstance(step, (int, float)) and not isinstance(step, bool):
            wait += step
            continue
        if isinstance(step, str):
            codes = [keycode(name) for name in step.split("+")]
            new = [(c, 1, MACRO_GAP_MS) for c in codes] + [(c, 0, MACRO_GAP_MS) for c in reversed(codes)]
        elif isinstance(step, dict) and "text" in step:
            new = _text_events(step["text"])
        elif isinstance(step, dict) and "events" in step:
            new = [(int(c), int(v), int(ms)) for c, v, ms in step["events"]]
        else:
            raise ValueError(f"unknown macro step {step!r}")
        if new and wait:
            new[0] = (new[0][0], new[0][1], new[0][2] + wait)
            wait = 0
        events += new
    return events


def play_macro(output, events):
    pressed = set()
    try:
        for code, value, delay in events:
            if delay:
                time.sleep(delay / 1000)
            output.send(code, value)
            (pressed.add if value else pressed.discard)(code)
    finally:
        for code in pressed:  # nothing may stay held
            output.send(code, 0)


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
            try:
                grabbed = swallow.grab()
            except OSError:  # something else is already holding it
                grabbed = False
            if grabbed:
                self.swallow = swallow
            else:
                for fd in swallow.fds:
                    os.close(fd)
                self._warn("These keys will keep typing: a key stayed held down, or something else is holding the keyboard.")

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
        self.keys = sorted({m.key for m in self.maps} | {m.chord for m in self.maps if m.chord}, key=lambda n: BY_NAME[n][0])
        self.held = sorted({m.key for m in self.maps if not m.chord and not m.ss_tap}, key=lambda n: BY_NAME[n][0])
        self.want_swallow = self.want_swallow and bool(self.held)  # shortcuts never need to grab the keyboard
        self.ss_taps = [m for m in self.maps if m.ss_tap]
        self.ss_held_for, self.ss_spoiled = None, False  # seconds SS has been down, and whether anything else was pressed
        self.pads = {}
        for player in sorted({m.player for m in self.maps if m.kind in ("stick", "trigger", "button")}):
            self.pads[player] = Gamepad(f"Apex Analog Player {player} (Xbox 360 layout)")
        kinds = {m.kind for m in self.maps}
        self.pedals = Pedals() if "pedal" in kinds else None
        self.mouse = Mouse() if kinds & {"mouse-axis", "mouse-button"} else None
        self.key_outputs = {m.target for m in self.maps if m.kind in ("key", "tap")}
        self.macros, self.macro_thread = {}, None
        wanted = {m.target for m in self.maps if m.kind == "macro"}
        if wanted:
            saved = _load_json(_config_path("macros.json"))
            missing = sorted(wanted - set(saved))
            if missing:
                raise ValueError(f"No macro called {' '.join(missing)}. Save one with: apex-analog macro add NAME STEP...")
            self.macros = {name: compile_macro(saved[name]) for name in wanted}
            self.key_outputs |= {code for events in self.macros.values() for code, _, _ in events}
        self.remainder = {}
        self.values = {}

    @property
    def pad(self):
        return self.pads.get(1)

    def held_keys(self):
        return self.held

    def read_keys(self):
        """Which keys to read next. While SS is down with a tap rule waiting, all of them, to see if it's part of a combo."""
        return None if self.ss_taps and self.ss_held_for is not None else self.keys

    def _ss_tap(self, depths, dt):
        """True on the frame SS comes back up after a quick press with nothing else pressed."""
        down = depths.get(SHORTCUT_HOLD, 0.0) >= SHORTCUT_DOWN
        others = any(mm >= SHORTCUT_DOWN for key, mm in depths.items() if key != SHORTCUT_HOLD)
        if down:
            if self.ss_held_for is None:
                self.ss_held_for, self.ss_spoiled = 0.0, others
            else:
                self.ss_held_for += dt
                self.ss_spoiled = self.ss_spoiled or others or self.ss_held_for > SS_TAP_SECONDS
            return False
        tapped = self.ss_held_for is not None and not self.ss_spoiled and not others and self.ss_held_for + dt <= SS_TAP_SECONDS
        self.ss_held_for = None
        return tapped

    def _fire(self, m):
        """A one-off: a keystroke pressed and let go, a macro, or a command."""
        if m.kind == "key" and self.virtual_keys:
            self.virtual_keys.send(m.target, 1)
            self.virtual_keys.send(m.target, 0)
        elif m.kind == "macro":
            self._play(m.target)
        elif m.kind == "run" and (m.process is None or m.process.poll() is not None):
            m.process = subprocess.Popen(m.target, shell=True, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def _depth(self, m, depths):
        """How far a rule's key counts as pressed. A shortcut's key only counts if SS went down first."""
        mm = depths.get(m.key, 0.0)
        if not m.chord:
            return mm
        if depths.get(m.chord, 0.0) < SHORTCUT_DOWN or not m.ready:
            m.ready = mm < SHORTCUT_DOWN
            return 0.0
        return mm

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
        if self.ss_taps and self._ss_tap(depths, dt):
            for m in self.ss_taps:
                self._fire(m)
        for m in self.maps:
            if m.ss_tap:
                continue
            mm = self._depth(m, depths)
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
            elif m.kind == "macro":
                state = m.digital.update(mm)
                if state and not m.active:
                    self._play(m.target)
                m.active = state
            elif m.kind == "tap":
                for _ in range(self._owed(m, amount * self.tap_rate, dt)):
                    if self.virtual_keys:
                        self.virtual_keys.send(m.target, 1)
                        self.virtual_keys.send(m.target, 0)
            elif m.kind == "run":
                if m.chord:  # once per press, however long it's held
                    state = m.digital.update(mm)
                    fire, m.active = state and not m.active, state
                else:
                    fire = self._owed(m, amount * self.run_rate, dt)
                if fire and (m.process is None or m.process.poll() is not None):
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

    def _play(self, name):
        """One macro at a time, so a held or repeated press can't pile them up. It never holds up the key loop."""
        if self.virtual_keys and not (self.macro_thread and self.macro_thread.is_alive()):
            self.macro_thread = threading.Thread(target=play_macro, args=(self.virtual_keys, self.macros[name]), daemon=True)
            self.macro_thread.start()

    def release(self):
        """Let go of everything: nothing may stay pressed once the keyboard is gone."""
        for m in self.maps:
            if m.digital:
                m.digital.down = False
        self.ss_held_for = None  # a keyboard that went away mid-press isn't a tap
        self.update({}, 0.0)
        self.stop_holding()

    def close(self):
        self.release()
        if self.macro_thread:
            self.macro_thread.join(10)
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


# ---- lighting effects ------------------------------------------------------------------------------------------------

def _scale(color, amount):
    return tuple(int(c * max(0.0, min(1.0, amount))) for c in color)


def _meter_color(t):
    """Green at the bottom of a meter, through yellow, to red at the top."""
    if t < 0.6:
        return (int(255 * t / 0.6), 200, 0)
    return (255, int(200 * (1 - (t - 0.6) / 0.4)), 0)


def _heat_color(t):
    """Blue for rarely used, through magenta and red, to yellow for most used."""
    if t < 0.5:
        return (int(510 * t), 0, int(255 * (1 - 2 * t)))
    return (255, int(400 * (t - 0.5)), 0)


class Effect:
    """A lighting effect: each frame says what colour keys should be. Keys it leaves out go dark, or back to normal with keep."""

    def __init__(self, keep=False, base=(0, 0, 0), hz=30.0):
        self.keep, self.base, self.period = keep, base, 1.0 / hz
        self.shown = {}
        self.last = self.last_clear = 0.0

    def start(self, kb):
        self.shown = {}
        self.last = self.last_clear = 0.0
        if self.keep:
            kb.clear_colors()
        else:
            kb.set_all_colors(*self.base)

    def frame(self, depths, now, elapsed):
        return {}

    def update(self, kb, depths, now):
        elapsed = now - self.last
        if elapsed < self.period:
            return
        self.last = now
        self.draw(kb, self.frame(depths, now, elapsed), now)

    def draw(self, kb, wanted, now):
        gone = set(self.shown) - set(wanted)
        changes = {}
        if self.keep:
            if gone and (not wanted or now - self.last_clear >= 0.2):  # the firmware can only release every key at once
                kb.clear_colors()
                self.last_clear = now
                self.shown = {}
            elif gone:
                wanted = {**{k: self.shown[k] for k in gone}, **wanted}
        else:
            changes = {k: self.base for k in gone}
        changes.update({k: v for k, v in wanted.items() if self.shown.get(k) != v})
        if changes:
            kb.set_colors(changes)
        self.shown = wanted

    def stop(self, kb):
        try:
            kb.clear_colors()
        except Disconnected:
            pass


class Ripple(Effect):
    """Pressing a key sends a wave across the keyboard. Pressing faster makes it stronger."""
    SPEED, WIDTH, LIFE = 24.0, 1.6, 0.9

    def __init__(self, color=(0, 170, 255), keep=False, base=(0, 0, 0)):
        super().__init__(keep, base, 25.0)
        self.color = color
        self.tracker = _Tracker(0.5, 0.3)
        self.waves = []

    def frame(self, depths, now, elapsed):
        for event in self.tracker.update(depths, now):
            if event.kind == "down" and event.key in POSITIONS:
                self.waves.append((POSITIONS[event.key], now, min(1.0, 0.35 + event.velocity / 250)))
        self.waves = [w for w in self.waves if now - w[1] < self.LIFE]
        wanted = {}
        for key, (x, y) in POSITIONS.items():
            level = depths.get(key, 0.0) / TRAVEL_MM
            for (wave_x, wave_y), started, strength in self.waves:
                age = now - started
                ring = max(0.0, 1 - abs(math.hypot(x - wave_x, y - wave_y) - age * self.SPEED) / self.WIDTH)
                level = max(level, ring * strength * (1 - age / self.LIFE))
            if level >= 0.04:
                wanted[key] = _scale(self.color, level)
        return wanted


def audio_bands(samples, rate=11025, bands=14, low=60.0, high=4500.0):
    """How loud each log-spaced frequency band is, 0 to about 0.5, measured with one Goertzel filter per band."""
    out = []
    count = len(samples)
    if not count:
        return [0.0] * bands
    for band in range(bands):
        frequency = low * (high / low) ** (band / (bands - 1))
        coefficient = 2 * math.cos(2 * math.pi * frequency / rate)
        previous = before = 0.0
        for sample in samples:
            previous, before = sample + coefficient * previous - before, previous
        power = max(0.0, previous * previous + before * before - coefficient * previous * before)
        out.append(math.sqrt(power) / count)
    return out


class AudioMeter(threading.Thread):
    """Listens to whatever this computer is playing, through the default output's monitor. Nothing is recorded or kept."""
    RATE, WINDOW = 11025, 256

    def __init__(self):
        super().__init__(daemon=True)
        self.levels = [0.0] * 14
        self.peak = 0.02
        self.process = None

    def run(self):
        command = ["parec", "--device=@DEFAULT_MONITOR@", f"--rate={self.RATE}", "--channels=1", "--format=s16le", "--latency-msec=30"]
        try:
            self.process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        except FileNotFoundError:
            _say("The music effect needs parec (pulseaudio-utils or pipewire-pulse).")
            return
        size = self.WINDOW * 2
        while True:
            data = self.process.stdout.read(size)
            if not data or len(data) < size:
                return
            samples = [s / 32768 for s in struct.unpack(f"<{self.WINDOW}h", data)]
            self.update_levels(audio_bands(samples, self.RATE, len(self.levels)))

    def update_levels(self, bands):
        self.peak = max(self.peak * 0.997, max(bands), 0.02)
        self.levels = [max(math.sqrt(b / self.peak) if b > 0.0015 else 0.0, old - 0.06) for b, old in zip(bands, self.levels)]

    def close(self):
        if self.process:
            self.process.terminate()


class MusicBars(Effect):
    """Bars across the keyboard that move with whatever is playing."""
    WIDTH_UNITS, HEIGHT_UNITS = 18.25, 5.25

    def __init__(self, keep=False, base=(0, 0, 0)):
        super().__init__(keep, base, 30.0)
        self.meter = None

    def start(self, kb):
        super().start(kb)
        if self.meter is None:
            self.meter = AudioMeter()
            self.meter.start()

    def bars(self, levels):
        wanted = {}
        for key, (x, y) in POSITIONS.items():
            column = min(len(levels) - 1, int(x / self.WIDTH_UNITS * len(levels)))
            height = 1 - y / self.HEIGHT_UNITS
            if levels[column] > 0.02 and height <= levels[column]:
                wanted[key] = _meter_color(height)
        return wanted

    def frame(self, depths, now, elapsed):
        return self.bars(self.meter.levels)

    def stop(self, kb):
        if self.meter:
            self.meter.close()
            self.meter = None
        super().stop(kb)


def _read_number(path):
    try:
        with open(path) as f:
            return float(f.read().strip())
    except (OSError, ValueError):
        return None


class SystemStats:
    """Each reading runs 0 to 1. A reading this machine doesn't have comes back as 0."""

    def __init__(self):
        self.cpu_last = None

    def cpu(self):
        with open("/proc/stat") as f:
            fields = [int(x) for x in f.readline().split()[1:]]
        idle, total = fields[3] + fields[4], sum(fields)
        last, self.cpu_last = self.cpu_last, (idle, total)
        if not last or total == last[1]:
            return 0.0
        return 1 - (idle - last[0]) / (total - last[1])

    def ram(self):
        info = {}
        with open("/proc/meminfo") as f:
            for line in f:
                name, _, value = line.partition(":")
                info[name] = int(value.split()[0])
        return 1 - info["MemAvailable"] / info["MemTotal"]

    def temp(self):
        for folder in glob.glob("/sys/class/hwmon/hwmon*"):
            if _read(f"{folder}/name") in ("coretemp", "k10temp", "zenpower"):
                value = _read_number(f"{folder}/temp1_input")
                if value:
                    return max(0.0, min(1.0, (value / 1000 - 30) / 70))
        value = _read_number("/sys/class/thermal/thermal_zone0/temp")
        return max(0.0, min(1.0, (value / 1000 - 30) / 70)) if value else 0.0

    def battery(self):
        for folder in glob.glob("/sys/class/power_supply/BAT*"):
            value = _read_number(f"{folder}/capacity")
            if value is not None:
                return value / 100
        return 0.0

    def gpu(self):
        try:
            out = subprocess.run(["nvidia-smi", "--query-gpu=utilization.gpu", "--format=csv,noheader,nounits"], capture_output=True, text=True, timeout=2).stdout
            return float(out.split()[0]) / 100
        except (OSError, ValueError, IndexError, subprocess.SubprocessError):
            return 0.0


STAT_ROWS = [[f"F{i}" for i in range(1, 13)], list("1234567890-="), list("QWERTYUIOP[]"), list("ASDFGHJKL;'")]
STAT_NAMES = ("cpu", "ram", "temp", "battery", "gpu")


class StatsMeter(Effect):
    """Rows of keys as meters: the first reading on the F row, then the number row, then down. Other keys stay as they are."""

    def __init__(self, names=("cpu", "ram"), stats=None):
        super().__init__(True, (0, 0, 0), 1.0)
        unknown = [n for n in names if n not in STAT_NAMES]
        if unknown or not 1 <= len(names) <= len(STAT_ROWS):
            raise ValueError(f"--stats takes up to {len(STAT_ROWS)} of: {' '.join(STAT_NAMES)}")
        self.names, self.stats = names, stats or SystemStats()
        self.stats.cpu()

    def frame(self, depths, now, elapsed):
        wanted = {}
        for name, row in zip(self.names, STAT_ROWS):
            lit = round(getattr(self.stats, name)() * len(row))
            for i, key in enumerate(row):
                wanted[key] = _meter_color(i / (len(row) - 1)) if i < lit else (0, 0, 0)
        return wanted


OUTGAUGE = struct.Struct("<I4sHBB7fII3f16s16s")
OUTGAUGE_SHIFT_LIGHT = 1


def parse_outgauge(data):
    """BeamNG's car data, in the OutGauge format. Returns (gear, rpm, shift light on) or None. Gear: 0 reverse, 1 neutral, 2 first."""
    if len(data) < OUTGAUGE.size:
        return None
    fields = OUTGAUGE.unpack_from(data)
    return fields[3], fields[6], bool(fields[13] & OUTGAUGE_SHIFT_LIGHT)


class RevLights(Effect):
    """Live RPM across the number row, flashing at the shift point, and the current gear on the F row, from BeamNG's OutGauge data.

    Turn it on in BeamNG under Options, Other, Protocols, with address 127.0.0.1 and the same port as here.
    """
    ROW = list("1234567890-=")

    def __init__(self, port=4444, redline=None):
        super().__init__(True, (0, 0, 0), 30.0)
        self.port, self.redline = port, redline
        self.highest = 0.0
        self.latest, self.received = None, 0.0
        self.socket = None

    def start(self, kb):
        super().start(kb)
        if self.socket is None:
            self.socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self.socket.setblocking(False)
            try:
                self.socket.bind(("127.0.0.1", self.port))
            except OSError as e:
                raise ApexError(f"Can't listen on UDP port {self.port}: {e}") from None

    def receive(self, now):
        while self.socket:
            try:
                packet = parse_outgauge(self.socket.recv(256))
            except OSError:
                return
            if packet:
                self.latest, self.received = packet, now

    def frame(self, depths, now, elapsed):
        self.receive(now)
        if not self.latest or now - self.received > 1.0:
            return {}
        gear, rpm, shift_light = self.latest
        self.highest = max(self.highest, rpm if rpm < 25000 else 0.0)
        redline = self.redline or max(self.highest, 3000.0)
        fraction = min(1.0, rpm / redline)
        wanted = {}
        if shift_light or fraction >= 0.93:
            flash = (255, 0, 0) if int(now * 8) % 2 else (255, 255, 255)
            wanted.update({key: flash for key in self.ROW})
        else:
            lit = round(fraction * len(self.ROW))
            for i, key in enumerate(self.ROW):
                wanted[key] = _meter_color(i / (len(self.ROW) - 1)) if i < lit else (0, 0, 0)
        if gear == 0:
            wanted["ESC"] = (255, 0, 0)
        elif 2 <= gear <= 13:
            wanted[f"F{gear - 1}"] = (255, 255, 255)
        return wanted

    def stop(self, kb):
        if self.socket:
            self.socket.close()
            self.socket = None
        super().stop(kb)


class TypingHeatmap(Effect):
    """Lights each key by how much you use it: your apex-analog profile totals, plus every press since it started."""

    def __init__(self, keep=False, base=(0, 0, 0), totals=None):
        super().__init__(keep, base, 10.0)
        if totals is None:
            path = _state_path("typing-profile.json")
            if not os.path.exists(path):
                raise ApexError("No typing profile yet. Run apex-analog profile and type for a while first.")
            with open(path) as f:
                totals = {k: v["count"] for k, v in json.load(f).get("keys", {}).items()}
        self.totals = totals
        self.log = _PressLog()

    def update(self, kb, depths, now):
        # Count on every reading, not just the ones that redraw, or quick presses would be missed.
        for press in self.log.update(depths, now):
            self.totals[press.key] = self.totals.get(press.key, 0) + 1
        super().update(kb, depths, now)

    def frame(self, depths, now, elapsed):
        top = max(self.totals.values(), default=0)
        if not top:
            return {}
        return {k: _heat_color(math.log(c + 1) / math.log(top + 1)) for k, c in self.totals.items() if c}


EFFECTS = ("glow", "ripple", "music", "stats", "rev", "heatmap")
USES_DEPTH = ("glow", "ripple", "heatmap")


def make_effect(name, color=None, base=(0, 0, 0), fade=None, keep=False, stats=("cpu", "ram"), port=4444, redline=None):
    if name == "glow":
        if keep:
            return GlowOverNormal(color or (255, 255, 255), 0.1 if fade is None else fade)
        return Lights(color or (255, 90, 0), base, 0.25 if fade is None else fade)
    if name == "ripple":
        return Ripple(color or (0, 170, 255), keep, base)
    if name == "music":
        return MusicBars(keep, base)
    if name == "stats":
        return StatsMeter(stats)
    if name == "rev":
        return RevLights(port, redline)
    if name == "heatmap":
        return TypingHeatmap(keep, base)
    raise ValueError(f"unknown effect {name!r}. Effects: {' '.join(EFFECTS)}")


# ---- presets and per-app profiles ------------------------------------------------------------------------------------

def _load_json(path):
    if not os.path.exists(path):
        return {}
    with open(path) as f:
        return json.load(f)


def apply_preset(kb, keys):
    """Put keys back to a saved {'W': [press, release, mode]}."""
    points, modes = {}, {}
    for key, (press, release, mode) in keys.items():
        if key in kb.keys:
            points.setdefault((press, release), []).append(key)
            modes.setdefault(mode, []).append(key)
    for (press, release), names in points.items():
        kb.set_actuation_points(names, press, release)
    for mode, names in modes.items():
        kb.set_mode(names, mode)


def _window_title():
    tool = shutil.which("kdotool") or os.path.expanduser("~/.cargo/bin/kdotool")
    out = subprocess.run([tool, "getactivewindow", "getwindowname"], capture_output=True, text=True, timeout=3)
    return out.stdout.strip()


AUTO_EXAMPLE = {
    "rules": [{"window": "BeamNG", "preset": "gaming", "lights": "ripple"}, {"window": "Firefox", "lights": "glow-keep"}],
    "default": {"preset": "typing", "lights": "glow-keep"},
}


class AutoProfiles:
    """Follows the focused window: applies a saved preset and a lighting effect for each app, from a small JSON file.

    Used in place of a Lights object. `lights` can be off, glow, glow-keep, ripple, ripple-keep, music, music-keep, stats, rev or heatmap.
    """

    def __init__(self, config, presets, title=None):
        self.rules, self.default = config.get("rules", []), config.get("default", {})
        self.presets, self.title = presets, title or _window_title
        self.window, self.current, self.effect, self.done = "", None, None, False
        self.warned = set()

    def choose(self, window):
        for rule in self.rules:
            if rule.get("window", "").lower() in window.lower():
                return rule
        return self.default

    def _poll(self):
        while not self.done:
            try:
                self.window = self.title()
            except (OSError, subprocess.SubprocessError):
                pass
            time.sleep(1.5)

    def start(self, kb):
        self.current = None
        self.done = False
        threading.Thread(target=self._poll, daemon=True).start()

    def apply(self, kb, rule):
        self.current = rule
        name = rule.get("preset")
        if name:
            if name in self.presets:
                apply_preset(kb, self.presets[name])
            elif name not in self.warned:
                self.warned.add(name)
                _say(f"No preset called {name!r}. Save one with: apex-analog preset save {name}")
        if self.effect:
            self.effect.stop(kb)
            self.effect = None
        lights = rule.get("lights", "off")
        if lights != "off":
            name, _, flag = lights.partition("-")
            self.effect = make_effect(name, keep=flag == "keep")
            self.effect.start(kb)

    def update(self, kb, depths, now):
        rule = self.choose(self.window)
        if rule is not self.current:
            self.apply(kb, rule)
        if self.effect:
            self.effect.update(kb, depths, now)

    def stop(self, kb):
        self.done = True
        if self.effect:
            self.effect.stop(kb)
            self.effect = None
        else:
            try:
                kb.clear_colors()
            except Disconnected:
                pass


# ---- drawing on the OLED ---------------------------------------------------------------------------------------------

FONT = {
    "0": "3E5149453E", "1": "00427F4000", "2": "4261514946", "3": "2141454B31", "4": "1814127F10", "5": "2745454539", "6": "3C4A494930", "7": "0171090503",
    "8": "3649494936", "9": "064949291E", "A": "7E1111117E", "B": "7F49494936", "C": "3E41414122", "D": "7F4141221C", "E": "7F49494941", "F": "7F09090901",
    "G": "3E4149497A", "H": "7F0808087F", "I": "00417F4100", "J": "2040413F01", "K": "7F08142241", "L": "7F40404040", "M": "7F020C027F", "N": "7F0408107F",
    "O": "3E4141413E", "P": "7F09090906", "Q": "3E4151215E", "R": "7F09192946", "S": "4649494931", "T": "01017F0101", "U": "3F4040403F", "V": "1F2040201F",
    "W": "3F4038403F", "X": "6314081463", "Y": "0708700807", "Z": "6151494543", ".": "0060600000", ",": "0050300000", "-": "0808080808", "=": "1414141414",
    "/": "2010080402", "\\": "0204081020", ";": "0056360000", "'": "0005030000", "`": "0001020400", "[": "007F414100", "]": "0041417F00", "#": "147F147F14",
    " ": "0000000000", "m": "7C04180478",
}
OLED_WIDTH, OLED_HEIGHT, OLED_BYTES = 128, 40, 640


class OledFrame:
    """A 128x40 picture in the panel's format: 5 pages of 128 columns, with bit N of a byte being pixel N of that page."""

    def __init__(self):
        self.data = bytearray(OLED_BYTES)

    def pixel(self, x, y):
        if 0 <= x < OLED_WIDTH and 0 <= y < OLED_HEIGHT:
            self.data[(y // 8) * OLED_WIDTH + x] |= 1 << (y % 8)

    def is_set(self, x, y):
        return bool(self.data[(y // 8) * OLED_WIDTH + x] >> (y % 8) & 1)

    def rect(self, x, y, width, height):
        for column in range(x, x + width):
            for row in range(y, y + height):
                self.pixel(column, row)

    def text(self, value, x, y, scale=1):
        for character in value:
            columns = bytes.fromhex(FONT.get(character, FONT[" "]))
            for i, bits in enumerate(columns):
                for row in range(7):
                    if bits >> row & 1:
                        self.rect(x + i * scale, y + row * scale, scale, scale)
            x += 6 * scale


def oled_depth_frame(name, mm, actuates_at=None):
    frame = OledFrame()
    frame.text(name, 0, 0, 2)
    frame.text(f"{mm:.1f}", 76, 0, 3)
    filled = int(OLED_WIDTH * mm / TRAVEL_MM)
    frame.rect(0, 28, filled, 11)
    for x in range(0, OLED_WIDTH, 2):
        frame.pixel(x, 27)
        frame.pixel(x, 39)
    if actuates_at is not None:
        mark = min(OLED_WIDTH - 1, int(OLED_WIDTH * actuates_at / TRAVEL_MM))
        frame.rect(mark, 24, 1, 16)
    return frame


def oled_map_frame(depths):
    frame = OledFrame()
    for key, (x, y) in POSITIONS.items():
        left, top = int(x * 6.9), int(y * 6.2)
        mm = depths.get(key)
        if mm is None:
            continue
        frame.pixel(left + 2, top + 5)
        if mm > 0:
            height = 1 + int(5 * mm / TRAVEL_MM)
            frame.rect(left, top + 6 - height, 5, height)
    return frame


def oled_socket_path():
    return os.path.join(os.environ.get("XDG_RUNTIME_DIR", "/tmp"), "apex-oledd.sock")


def oled_send(frame, seconds=0.6, path=None):
    """Hand a frame to apex-oledd, which decides what's on the panel, so this never fights it. It lasts `seconds` unless replaced."""
    message = {"cmd": "notify", "secs": seconds, "invert": False, "fb": base64.b64encode(bytes(frame.data)).decode()}
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(0.5)
        client.connect(path or oled_socket_path())
        client.sendall((json.dumps(message) + "\n").encode())
        try:
            client.recv(4096)
        except socket.timeout:
            pass


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


# ---- the daemon ------------------------------------------------------------------------------------------------------

OP_OPEN, OP_ASK, OP_WRITE, OP_JOB, OP_OFF, OP_STATUS, OP_QUIT = range(7)
STATUS_OK, STATUS_LOST, STATUS_NOT_FOUND, STATUS_DENIED, STATUS_ERROR = range(5)
_FRAME = struct.Struct("<BI")
# What each long-running command occupies. Starting another in the same slot replaces it.
SLOTS = {"shortcuts": "shortcuts", "lights": "lights", "auto": "lights", "gamepad": "input", "map": "input", "midi": "input", "type": "input", "overlay": "overlay", "oled": "oled", "stream": "stream"}
DEFAULT_LIGHTS = ["lights", "--keep"]


def _socket_path():
    return os.path.join(os.environ.get("XDG_RUNTIME_DIR") or "/tmp", "apex-analog.sock")


def _in_job():
    return bool(os.environ.get("APEX_ANALOG_JOB"))


def _recv_exact(sock, count):
    data = b""
    while len(data) < count:
        chunk = sock.recv(count - len(data))
        if not chunk:
            raise ConnectionError("closed")
        data += chunk
    return data


class DaemonClient:
    """One connection to the daemon. Each call gets its own answer, so any number of programs can share the keyboard."""

    def __init__(self, path=None):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            self.sock.settimeout(15)
            self.sock.connect(path or _socket_path())
        except OSError:
            self.sock.close()
            raise

    def call(self, op, data=b""):
        try:
            self.sock.sendall(_FRAME.pack(op, len(data)) + data)
            status, size = _FRAME.unpack(_recv_exact(self.sock, _FRAME.size))
            reply = _recv_exact(self.sock, size)
        except OSError as e:
            raise Disconnected(f"Lost the apex-analog daemon: {e}") from None
        if status == STATUS_OK:
            return reply
        message = reply.decode(errors="replace")
        raise {STATUS_LOST: Disconnected, STATUS_NOT_FOUND: NotFound, STATUS_DENIED: AccessDenied}.get(status, ApexError)(message)

    def close(self):
        self.sock.close()


def daemon_client(start=True):
    """Connect to the daemon, starting it if asked and needed. None if it can't be reached."""
    try:
        return DaemonClient()
    except OSError:
        if not start:
            return None
    subprocess.Popen([sys.executable, os.path.abspath(__file__), "daemon"], start_new_session=True,
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    deadline = time.monotonic() + 3.0
    while time.monotonic() < deadline:
        try:
            return DaemonClient()
        except OSError:
            time.sleep(0.05)
    return None


class Daemon:
    """Owns the keyboard and hands out access to it, runs long-lived commands as its own jobs, and brings the right ones back at login."""
    READ_CACHE = 0.003  # readers asking for the same bank this close together share one read

    def __init__(self, path=None, open_keyboard=None, spawn=None, state_path=None):
        self.path = path or _socket_path()
        self.open_keyboard = open_keyboard or (lambda: Keyboard(direct=True))
        self.spawn = spawn or (lambda argv: [sys.executable, os.path.abspath(__file__), *argv])
        self.state_path = state_path or _config_path("daemon.json")
        self.lock, self.job_lock = threading.RLock(), threading.RLock()
        self.done = threading.Event()
        self.kb, self.jobs, self.cache = None, {}, {}
        self.server = None

    # keyboard access
    def keyboard(self):
        if self.kb is None:
            self.kb = self.open_keyboard()
        return self.kb

    def lost(self):
        with self.lock:
            if self.kb is not None:
                self.kb.close()
            self.kb = None
            self.cache = {}

    def ask(self, payload):
        if not payload or payload[0] not in ALLOWED:
            raise ApexError(f"refused to send {payload[:1]!r}: not a known read command")
        with self.lock:
            now = time.monotonic()
            if payload[0] == 0xD7 and payload in self.cache and now - self.cache[payload][0] < self.READ_CACHE:
                return self.cache[payload][1]
            reply = self.keyboard().exchange_once(payload)
            if payload[0] == 0xD7 and _valid_bank(reply):
                self.cache[payload] = (time.monotonic(), reply)
            return reply

    def write(self, payload):
        with self.lock:
            self.cache = {}
            self.keyboard()._write(list(payload))
        return b""

    # jobs
    def _state(self):
        return _load_json(self.state_path)

    def _remember(self, **values):
        try:
            _write_private(self.state_path, {**self._state(), **values})
        except OSError:
            pass

    def start_job(self, slot, label, argv, cwd=None):
        with self.job_lock:
            self.stop_job(slot)
            log_path = os.path.join(os.environ.get("XDG_RUNTIME_DIR") or "/tmp", f"apex-analog-{slot}.log")
            with open(log_path, "w") as log:
                process = subprocess.Popen(self.spawn(argv), start_new_session=True, stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                                           cwd=cwd if cwd and os.path.isdir(cwd) else None, env={**os.environ, "APEX_ANALOG_JOB": "1"})
            self.jobs[slot] = {"process": process, "label": label, "argv": argv, "started": time.time(), "log": log_path}
            deadline = time.monotonic() + 0.8
            while time.monotonic() < deadline and process.poll() is None:  # most mistakes show up straight away
                time.sleep(0.03)
            ok = process.poll() is None or process.returncode == 0
            if slot == "lights" and ok:
                self._remember(lights=argv)
            if slot == "recording":
                self._remember(recording=True)
            if slot == "shortcuts" and ok:
                self._remember(shortcuts=True)
            return {"ok": ok, "log": _read(log_path) or ""}

    def stop_job(self, slot):
        with self.job_lock:
            job = self.jobs.pop(slot, None)
            if job and job["process"].poll() is None:
                job["process"].terminate()
                try:
                    job["process"].wait(timeout=3)
                except subprocess.TimeoutExpired:
                    job["process"].kill()
                    job["process"].wait()
            return bool(job)

    def turn_off(self, slot):
        with self.job_lock:
            found = self.stop_job(slot)
            if slot == "lights":
                self._remember(lights=None)
            if slot == "recording":
                self._remember(recording=False)
            if slot == "shortcuts":
                self._remember(shortcuts=False)
            return found

    def status(self):
        with self.job_lock:
            jobs = {}
            for slot, job in self.jobs.items():
                running = job["process"].poll() is None
                jobs[slot] = {"label": " ".join(job["argv"]), "running": running, "seconds": int(time.time() - job["started"]),
                              "log": "" if running else (_read(job["log"]) or "")[-200:]}
        connected = self.kb is not None
        return {"pid": os.getpid(), "keyboard": connected, "firmware": self.kb.firmware if connected else None, "jobs": jobs}

    # the connection to each program
    def handle(self, op, data):
        if op == OP_ASK:
            return self.ask(data)
        if op == OP_WRITE:
            return self.write(data)
        if op == OP_OPEN:
            with self.lock:
                kb = self.keyboard()
                return json.dumps({"node": kb.node, "firmware": kb.firmware}).encode()
        if op == OP_JOB:
            request = json.loads(data)
            return json.dumps(self.start_job(request["slot"], request["label"], request["argv"], request.get("cwd"))).encode()
        if op == OP_OFF:
            return json.dumps({"found": self.turn_off(data.decode())}).encode()
        if op == OP_STATUS:
            return json.dumps(self.status()).encode()
        if op == OP_QUIT:
            self.done.set()
            return b"{}"
        raise ApexError(f"unknown request {op}")

    def serve(self, connection):
        with connection:
            try:
                while True:
                    op, size = _FRAME.unpack(_recv_exact(connection, _FRAME.size))
                    data = _recv_exact(connection, size)
                    try:
                        status, reply = STATUS_OK, self.handle(op, data)
                    except Disconnected as e:
                        self.lost()
                        status, reply = STATUS_LOST, str(e).encode()
                    except NotFound as e:
                        status, reply = STATUS_NOT_FOUND, str(e).encode()
                    except AccessDenied as e:
                        status, reply = STATUS_DENIED, str(e).encode()
                    except (ApexError, ValueError) as e:
                        status, reply = STATUS_ERROR, str(e).encode()
                    connection.sendall(_FRAME.pack(status, len(reply)) + reply)
            except OSError:
                pass

    def _accept(self):
        while not self.done.is_set():
            try:
                connection, _ = self.server.accept()
            except OSError:
                return
            threading.Thread(target=self.serve, args=(connection,), daemon=True).start()

    def listen(self):
        if os.path.exists(self.path):
            try:
                DaemonClient(self.path).close()
                return False  # one is already running
            except OSError:
                os.remove(self.path)
        self.server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.server.bind(self.path)
        os.chmod(self.path, 0o600)
        self.server.listen(16)
        threading.Thread(target=self._accept, daemon=True).start()
        return True

    def start_saved_jobs(self):
        state = self._state()
        lights = state.get("lights", DEFAULT_LIGHTS)
        if lights:
            self.start_job("lights", lights[0], lights)
        if state.get("recording", True):
            self.start_job("recording", "recording", ["record"])
        if state.get("shortcuts", True) and _load_json(os.path.join(os.path.dirname(self.state_path), "shortcuts.json")):
            self.start_job("shortcuts", "shortcuts", ["shortcuts"])

    def shutdown(self):
        self.done.set()
        with self.job_lock:
            for slot in list(self.jobs):
                self.stop_job(slot)
        if self.server:
            self.server.close()
        try:
            os.remove(self.path)
        except OSError:
            pass
        self.lost()


def cmd_daemon(args):
    daemon = Daemon()
    if not daemon.listen():
        return 0
    try:
        daemon.start_saved_jobs()
        daemon.done.wait()
    finally:
        daemon.shutdown()
    return 0


def _job_call(client, op, value):
    return json.loads(client.call(op, value if isinstance(value, bytes) else json.dumps(value).encode()))


def _detach(args, argv):
    """Hand a long-running command to the daemon, so the terminal comes straight back."""
    client = daemon_client()
    if client is None:
        raise ApexError("Couldn't start the apex-analog daemon. Run the command with --watch to run it in this terminal.")
    try:
        result = _job_call(client, OP_JOB, {"slot": SLOTS[args.command], "label": args.command, "argv": argv, "cwd": os.getcwd()})
    finally:
        client.close()
    if not result["ok"]:
        raise ApexError(result["log"].strip().replace("apex-analog: ", "", 1) or "It stopped straight away.")
    if result["log"].strip():
        print(result["log"].strip())
    print(f"Running in the background. Turn it off with: apex-analog off {args.command}")
    return 0


def cmd_off(args):
    client = daemon_client(start=False)
    if client is None:
        print("The daemon isn't running, so nothing is on.")
        return 0
    try:
        status = _job_call(client, OP_STATUS, b"")
        slots = list(status["jobs"]) if args.name == "all" else [SLOTS.get(args.name, args.name)]
        found = [slot for slot in slots if _job_call(client, OP_OFF, slot.encode())["found"]]
    finally:
        client.close()
    print((f"Turned off {args.name}." if args.name != "all" else f"Turned off: {' '.join(found)}") if found else f"{args.name} wasn't on.")
    return 0


def cmd_status(args):
    client = daemon_client(start=False)
    if client is None:
        print("The daemon isn't running. Any apex-analog command starts it.")
        return 0
    try:
        status = _job_call(client, OP_STATUS, b"")
    finally:
        client.close()
    print(f"Daemon running, process {status['pid']}. Keyboard: {'connected, firmware ' + status['firmware'] if status['keyboard'] else 'not connected yet'}.")
    if not status["jobs"]:
        print("Nothing else is on.")
    for slot, job in sorted(status["jobs"].items()):
        minutes = job["seconds"] // 60
        print(f"{slot:11}{'on' if job['running'] else 'STOPPED':8}{job['label']}  ({minutes} min)" + (f"\n           {job['log'].strip()}" if job["log"].strip() else ""))
    return 0


def cmd_stop(args):
    client = daemon_client(start=False)
    if client is None:
        print("Nothing to stop.")
        return 0
    try:
        client.call(OP_QUIT)
    except ApexError:
        pass
    finally:
        client.close()
    deadline = time.monotonic() + 5.0
    while os.path.exists(_socket_path()) and time.monotonic() < deadline:
        time.sleep(0.05)
    print("Stopped the daemon and everything it was running. Your normal lighting is back. Any apex-analog command starts it again.")
    return 0


def cmd_record(args):
    """The always-on job behind apex-analog profile."""
    while True:
        try:
            with Keyboard() as kb:
                press_points = {k: v["press"] for k, v in kb.actuation().items()}
            break
        except (NotFound, AccessDenied, Disconnected):
            time.sleep(5)
    recorder = TypingRecorder(press_points)
    try:
        _loop(args.hz or 60, None, None, recorder.update)
    finally:
        recorder.save()
    return 0


HELP_TEXT = """apex-analog: see how far every key is pressed on an Apex Pro Gen 3, and use it for anything.

A small daemon owns the keyboard, and it starts by itself. Commands marked * run in the background and give
your terminal back, and several can run at once. Add --watch to keep one in the terminal instead.

  See what's happening
    bars                 live bars for every key you press (just apex-analog does this too)
    status               what's on right now
    info                 the keyboard, your access, and every key's settings
    health               check every switch for drift and noise
    profile              how you type, and what it means for actuation. Always recording
    magnet               every sensor, for playing with a magnet

  Use the keys
  * gamepad              W S A D as an analog controller
  * map RULE...          any key to a controller, pedals, the mouse, a keystroke, a macro, or a command
  * midi                 play it as an instrument
  * type                 press deep for capitals
    macro add|record|run save and replay keystrokes. Fire one with: map J=macro:NAME@3.0
    shortcut add KEY OUT hold the SteelSeries key and press KEY to fire OUT, like: shortcut add J volume+
  * shortcuts            run the saved shortcuts. shortcut add starts this for you
    typeprint            experiment: recognise you by how you press

  Lighting and screen
  * lights               glow with depth. --keep stays over normal lighting. --off goes back to normal
                         --effect ripple, music, stats, rev or heatmap
  * auto                 switch presets and lighting by the focused window
  * oled                 key depth on the keyboard's own screen
    paint                set key colours from any program

  Keyboard settings, no GG needed
    actuation KEYS MM    where keys fire, like: actuation WASD 1.2
    rapid KEYS on|off    the keyboard's own rapid tap
    bind KEY ACTION      what a key does, on the keyboard itself, like: bind CAPS esc. --layer ss or deep too
    bindings             what every key does
    layer-key KEY        move the SteelSeries layer to another key, so the SteelSeries key itself can be bound
    preset save|use NAME keep setups on this computer
    save                 write the current settings to the keyboard's flash, so they survive a replug

  Wire it into your own thing
    stream               one JSON line per update, to a pipe or terminal
  * stream --udp, --osc, --serve   the same over the network, on chosen keys
  * overlay              key pressure as an OBS browser source

  Turn things off
    off NAME             one thing: gamepad, lights, overlay, oled, stream, recording, or all
    stop                 the daemon and everything it runs
    autostart on|off     start the daemon at login

Details on any command: apex-analog help COMMAND
"""


def cmd_help(args):
    if not args.topic:
        print(HELP_TEXT, end="")
        return 0
    commands = build_parser().command_parsers
    if args.topic not in commands:
        raise ValueError(f"No command called {args.topic!r}. See them all with: apex-analog help")
    commands[args.topic].print_help()
    return 0


def bar_lines(pressed, settings, columns, rows):
    """Every pressed key, in as many columns as it takes to fit the terminal."""
    if not pressed:
        return ["Press a key slowly."]
    rows = max(1, rows)
    count = math.ceil(len(pressed) / rows)
    width = columns // count
    bar = max(6, min(40, width - 17))
    cells = [f"{name:7}|{_bar(mm, settings[name]['press'], bar)}| {mm:3.1f} mm" for mm, name in pressed]
    return ["".join(cell.ljust(width) for cell in cells[row::rows]).rstrip() for row in range(min(rows, len(cells)))]


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
            size = shutil.get_terminal_size()
            lines += bar_lines(pressed, settings, size.columns, size.lines - 3)
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
    if not _in_job():
        Keyboard().close()  # a problem at the start should be reported. A job has no one to report to, so it just waits
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
            if isinstance(e, AccessDenied) and not waiting and not _in_job():
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


def _rgb_any(value):
    if isinstance(value, str):
        return _rgb(value)
    if isinstance(value, (list, tuple)) and len(value) == 3 and all(isinstance(c, int) and 0 <= c <= 255 for c in value):
        return tuple(value)
    raise ValueError(f"{value!r} should be a colour like \"FF5A00\" or [255, 90, 0]")


def _lights_from(args):
    return make_effect(args.effect, _rgb(args.color) if args.color else None, _rgb(args.base), args.fade, args.keep,
                       tuple(args.stats.split(",")), args.port, args.redline)


def cmd_lights(args):
    if args.off:
        client = daemon_client(start=False)
        if client:
            try:
                client.call(OP_OFF, b"lights")
            finally:
                client.close()
        with Keyboard() as kb:
            kb.clear_colors()
        print("Back to your normal lighting.")
        return 0
    lights = _lights_from(args)
    keys = key_names(args.keys) if args.keys else (None if args.effect in USES_DEPTH else ["W"])
    with _Screen() as screen:
        screen.draw([f"apex-analog lights: {args.effect}", "", "Ctrl-C quits and restores your normal lighting."])
        _loop(args.hz or 60, keys, None, lambda depths, now: None, lights)


def cmd_paint(args):
    """Colours from any program: one JSON object per line, like {"W": "FF0000", "ESC": [0, 255, 0]}, {"all": "000000"}, {"clear": true}."""
    with Keyboard() as kb:
        try:
            for line in sys.stdin:
                line = line.strip()
                if not line:
                    continue
                message = json.loads(line)
                if message.pop("clear", False):
                    kb.clear_colors()
                if "all" in message:
                    kb.set_all_colors(*_rgb_any(message.pop("all")))
                if message:
                    kb.set_colors({key: _rgb_any(value) for key, value in message.items()})
        finally:
            if not args.stay:
                kb.clear_colors()
    return 0


def cmd_rapid(args):
    with Keyboard() as kb:
        if args.keys is not None:
            if args.state is None:
                raise ValueError("Say on or off too, like: apex-analog rapid WASD on")
            names = list(kb.keys) if args.keys == "all" else key_names(args.keys)
            missing = [n for n in names if n not in kb.keys]
            if missing:
                raise ValueError(f"No sensor fitted on this keyboard for: {' '.join(missing)}")
            mode = RAPID_TAP_MODE if args.state == "on" else 0
            kb.set_mode(names, mode)
            time.sleep(0.1)
            settings = kb.actuation(names)
            wrong = [n for n in names if settings[n]["mode"] != mode]
            if wrong:
                raise ApexError(f"Sent, but these read back differently: {' '.join(wrong)}")
            print(f"Rapid tap {args.state} for {len(names)} key{'s' if len(names) != 1 else ''}, and read it back.")
        settings = kb.actuation()
    on = [n for n, s in settings.items() if s["mode"] == RAPID_TAP_MODE]
    print("Rapid tap on: " + (" ".join(on) if on else "no keys"))
    return 0


def _binding_entry(record, default):
    return {"action": describe_binding(record), "raw": record.hex(), "default": record == default}


def read_bindings(kb, layers=(0, 1, 2)):
    """Everything bound on the keyboard, as `bindings --json` prints it."""
    layer_key = kb.layer_key()
    report = {"layer_key": BIND_NAMES.get(layer_key, f"{layer_key:#04x}"), "layers": {}, "wheel": {}}
    for layer in layers:
        report["layers"][LAYER_NAMES[layer]] = {name: _binding_entry(record, default_binding(layer, BIND_HIDS[name]))
                                                for name, record in kb.bindings(layer).items()}
        if layer in (0, 1):
            report["wheel"][LAYER_NAMES[layer]] = {direction: _binding_entry(record, default) for direction, record, default
                                                   in zip(WHEEL_DIRECTIONS, kb.wheel(layer), DEFAULT_WHEEL[layer])}
    return report


def cmd_bindings(args):
    layers = tuple(LAYERS.values()) if args.layer == "all" else (LAYERS[args.layer],)
    with Keyboard() as kb:
        report = read_bindings(kb, layers)
    if args.json:
        print(json.dumps(report))
        return 0
    for layer in layers:
        name = LAYER_NAMES[layer]
        entries = {**report["layers"][name], **report["wheel"].get(name, {})}
        shown = {k: v for k, v in entries.items() if args.full or not v["default"] or (layer == 1 and v["action"] != "none")}
        print(f"{LAYER_LABELS[layer][:1].upper()}{LAYER_LABELS[layer][1:]}:")
        if not shown:
            print("  every key does what it normally does" if layer == 0 else "  nothing bound")
        for key, entry in shown.items():
            print(f"  {key:13}{entry['action']}" + ("" if entry["default"] else "   (changed)"))
    if report["layer_key"] != "SS":
        print(f"The SteelSeries layer is on while {report['layer_key']} is held.")
    return 0


def cmd_layer_key(args):
    with Keyboard() as kb:
        if args.key:
            key = bind_key(args.key)
            if key in WHEEL_DIRECTIONS or key in WHEEL_KEY_HIDS:
                raise ValueError("Pick a key on the keyboard for the SteelSeries layer, not a control by the wheel.")
            kb.set_layer_key(key)
            time.sleep(0.1)
            if kb.layer_key() != BIND_HIDS[key]:
                raise ApexError("Sent, but it reads back differently.")
            if key == SHORTCUT_HOLD:
                print("The SteelSeries layer is back on the SteelSeries key.")
            else:
                print(f"Holding {key} now turns on the SteelSeries layer, so {key} stops doing its own thing. The SteelSeries key works "
                      f"like any other key: bind it with apex-analog bind SS ACTION. apex-analog layer-key SS puts it back. "
                      f"It lasts until you unplug the keyboard. apex-analog save keeps it.")
            return 0
        hid = kb.layer_key()
    print(f"Holding {BIND_NAMES.get(hid, f'{hid:#04x}')} turns on the SteelSeries layer.")
    return 0


def cmd_bind(args):
    layer = LAYERS[args.layer]
    key = bind_key(args.key)
    with Keyboard() as kb:
        if key in WHEEL_DIRECTIONS:
            if layer == 2:
                raise ValueError("The wheel has no deeper press.")
            records = list(kb.wheel(layer))
            i = WHEEL_DIRECTIONS.index(key)
            records[i] = DEFAULT_WHEEL[layer][i] if args.action == "default" else parse_binding(args.action)
            kb.set_wheel(layer, *records)
            time.sleep(0.1)
            if list(kb.wheel(layer)) != records:
                raise ApexError("Sent, but the wheel reads back differently.")
            record = records[i]
        else:
            hid = BIND_HIDS[key]
            if layer == 2 and hid not in DEEP_HIDS:
                raise ValueError(f"{key} has no sensor, so it has no deeper press.")
            if layer == 0 and hid == kb.layer_key():
                raise ValueError(f"{key} turns on the SteelSeries layer while it's held, so a normal binding on it never fires. "
                                 f"Bind it with --layer ss instead, which fires each time it's pressed, or move the layer to another "
                                 f"key first with apex-analog layer-key KEY.")
            record = default_binding(layer, hid) if args.action == "default" else parse_binding(args.action)
            kb.set_bindings(layer, {key: record})
            time.sleep(0.1)
            if kb.bindings(layer, [key])[key] != record:
                if layer == 2:
                    raise ApexError(f"The keyboard refused it: {key}'s deeper press starts shallower than where it first fires. "
                                    f"Move it with apex-analog actuation {key} 3.0 --deep.")
                raise ApexError("Sent, but it reads back differently.")
            if layer == 2 and record != bytes(5) and kb.deep_actuation([key])[key] is None:
                print(f"{key}'s deeper press is still at the very bottom. Choose where it starts with apex-analog actuation {key} 3.0 --deep.")
    if args.action == "default":
        print(f"{key} is back to what it does out of the box {LAYER_LABELS[layer]}: {describe_binding(record)}. The keyboard reads it back.")
    else:
        print(f"{key} now does {describe_binding(record)} {LAYER_LABELS[layer]}, and the keyboard reads it back. "
              f"It lasts until you unplug the keyboard. apex-analog save keeps it.")
    return 0


def cmd_preset(args):
    path = _config_path("presets.json")
    presets = _load_json(path)
    if args.action == "list":
        print("\n".join(sorted(presets)) if presets else "No presets yet. Save one with: apex-analog preset save NAME")
        return 0
    if not args.name:
        raise ValueError(f"Name the preset: apex-analog preset {args.action} NAME")
    if args.action == "delete":
        if args.name not in presets:
            raise ValueError(f"No preset called {args.name!r}")
        del presets[args.name]
        _write_private(path, presets)
        print(f"Deleted {args.name}.")
        return 0
    with Keyboard() as kb:
        if args.action == "save":
            settings = kb.actuation()
            presets[args.name] = {k: [s["press_raw"], s["release_raw"], s["mode"]] for k, s in settings.items()}
            _write_private(path, presets)
            print(f"Saved {args.name} on this computer, with {len(settings)} keys. The keyboard's flash wasn't touched.")
            return 0
        if args.name not in presets:
            raise ValueError(f"No preset called {args.name!r}. You have: {' '.join(sorted(presets)) or 'none'}")
        apply_preset(kb, presets[args.name])
        time.sleep(0.1)
        settings = kb.actuation()
        wrong = [k for k, (press, release, mode) in presets[args.name].items()
                 if k in settings and k != "LGUI" and (settings[k]["press_raw"], settings[k]["release_raw"], settings[k]["mode"]) != (press, release, mode)]
        if wrong:
            raise ApexError(f"Sent, but these read back differently: {' '.join(wrong)}")
        print(f"Using {args.name}.")
    return 0


def cmd_save(args):
    with Keyboard() as kb:
        slot = args.slot or kb.active_slot()
        if not 1 <= slot <= 5:
            raise ApexError(f"The keyboard reports config slot {slot}, which isn't 1 to 5. Not writing anything.")
        print(f"This writes all the keyboard's current settings to its flash, replacing saved config {slot}.")
        print(f"It sends one command: 09 {slot:02X}. The keyboard's own Save Config menu runs the same code.")
        if not args.yes and input("Type yes to write: ").strip() != "yes":
            print("Nothing written.")
            return 1
        kb.save_to_keyboard(slot)
        print(f"Saved to config {slot}, and the keyboard is answering again.")
    return 0


def cmd_auto(args):
    path = args.config or _config_path("auto.json")
    if args.example:
        print(json.dumps(AUTO_EXAMPLE, indent=2))
        return 0
    if not os.path.exists(path):
        raise ApexError(f"No rules yet. Put them in {path}. See an example with: apex-analog auto --example")
    with open(path) as f:
        config = json.load(f)
    auto = AutoProfiles(config, _load_json(_config_path("presets.json")))
    _say(f"Following the focused window with {len(auto.rules)} rule{'s' if len(auto.rules) != 1 else ''}. Ctrl-C quits and restores your normal lighting.")
    _loop(args.hz or 60, None, None, lambda depths, now: None, auto)


def _record_macro():
    """Listens to the keyboard without taking it over. Ends on the Pause key."""
    fds = []
    for node in _physical_key_nodes():
        fd = os.open(node, os.O_RDONLY | os.O_NONBLOCK)
        if BY_NAME["W"][2] in _key_bits(fd):
            fds.append(fd)
        else:
            os.close(fd)
    if not fds:
        raise ApexError("Couldn't find the keyboard's input devices.")
    try:
        while any(_any_key_held(fd) for fd in fds):
            time.sleep(0.02)
        size, events, last = struct.calcsize("llHHi"), [], None
        pause = keycode("PAUSE")
        while True:
            for fd in select.select(fds, [], [], 0.2)[0]:
                data = os.read(fd, size * 64)
                for offset in range(0, len(data) - size + 1, size):
                    _, _, kind, code, value = struct.unpack_from("llHHi", data, offset)
                    if kind != EV_KEY or value == 2:
                        continue
                    if code == pause:
                        return events
                    now = time.monotonic()
                    events.append([code, value, 0 if last is None else int((now - last) * 1000)])
                    last = now
    finally:
        for fd in fds:
            os.close(fd)


def cmd_macro(args):
    path = _config_path("macros.json")
    macros = _load_json(path)
    if args.action == "list":
        print("\n".join(f"{name}: {len(compile_macro(steps))} key events" for name, steps in sorted(macros.items())) or "No macros yet. Try: apex-analog macro add hello text:hi")
        return 0
    if not args.name:
        raise ValueError(f"Name the macro: apex-analog macro {args.action} NAME")
    if args.action == "add":
        steps = []
        for step in args.steps:
            if step.startswith("text:"):
                steps.append({"text": step[5:]})
            elif step.replace(".", "", 1).isdigit():
                steps.append(float(step))
            else:
                steps.append(step)
        if not steps:
            raise ValueError('Give it some steps: a combo like LCTRL+C, a wait in ms like 100, or text:"hello there"')
        compile_macro(steps)
        macros[args.name] = steps
        _write_private(path, macros)
        print(f"Saved {args.name}. Fire it with: apex-analog map J=macro:{args.name}, or macro:{args.name}@3.0 for a deep press only.")
        return 0
    if args.action == "record":
        print("Recording what you press, in order, into a private file on this computer, so don't type passwords. Press Pause to finish.", flush=True)
        with _Screen():
            events = _record_macro()
        macros[args.name] = [{"events": events}]
        _write_private(path, macros)
        print(f"Saved {args.name}: {len(events)} key events.")
        return 0
    if args.name not in macros:
        raise ValueError(f"No macro called {args.name!r}. You have: {' '.join(sorted(macros)) or 'none'}")
    if args.action == "delete":
        del macros[args.name]
        _write_private(path, macros)
        print(f"Deleted {args.name}.")
        return 0
    events = compile_macro(macros[args.name])
    with VirtualKeys({code for code, _, _ in events}) as output:
        time.sleep(0.3)  # let the desktop notice the new keyboard
        play_macro(output, events)
    return 0


def _autostart_path():
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return os.path.join(base, "autostart", "apex-analog.desktop")


def cmd_autostart(args):
    path = _autostart_path()
    if args.action == "off":
        if os.path.exists(path):
            os.remove(path)
        print("It won't start at login any more. A copy that's already running keeps going until apex-analog stop.")
        return 0
    if args.action == "status":
        if not os.path.exists(path):
            print("Off.")
            return 0
        with open(path) as f:
            print("On: " + next((l[5:].strip() for l in f if l.startswith("Exec=")), "unknown"))
        return 0
    command = args.command_args or ["daemon"]
    program = shutil.which("apex-analog") or os.path.abspath(__file__)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(f"[Desktop Entry]\nType=Application\nName=apex-analog\nComment=Key depth for the Apex Pro Gen 3\nExec={program} {' '.join(command)}\nTerminal=false\n")
    print(f"At your next login it will run: apex-analog {' '.join(command)}")
    return 0


def cmd_oled(args):
    if not os.path.exists(oled_socket_path()):
        raise ApexError("apex-oledd isn't running. This hands its frames to that daemon so the two never fight over the panel.")
    with Keyboard() as kb:
        settings = kb.actuation()
        _say("Press a key and watch the OLED. It goes back to its normal view when you let go. Ctrl-C quits.")
        last, sent = None, 0.0
        for depths in kb.stream(args.hz or 30):
            now = time.monotonic()
            pressed = [(mm, name) for name, mm in depths.items() if mm > 0]
            if args.map:
                frame = oled_map_frame(depths)
            elif pressed:
                mm, name = max(pressed)
                frame = oled_depth_frame(name, mm, settings[name]["press"])
            else:
                continue
            data = bytes(frame.data)
            if data != last or now - sent >= 0.3:
                try:
                    oled_send(frame)
                except OSError as e:
                    raise ApexError(f"Lost apex-oledd: {e}") from None
                last, sent = data, now


def cmd_magnet(args):
    with Keyboard() as kb, _Screen() as screen:
        if not screen.active:
            raise ApexError("The magnet view needs a terminal.")
        screen.draw(["Measuring the resting field. Keep magnets away for a second."])
        samples = []
        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline:
            samples.append(kb.channels())
        baseline = {c: statistics.fmean(s[c] for s in samples) for c in samples[0]}
        scans, rate, last = 0, 0.0, time.monotonic()
        while True:
            values = kb.channels()
            scans += 1
            now = time.monotonic()
            if now - last >= 1:
                rate, scans, last = scans / (now - last), 0, now
            deltas = {c: values[c] - baseline[c] for c in values}
            strongest = max(deltas, key=lambda c: abs(deltas[c]))
            lines = [f"apex-analog magnet  {rate:.0f} updates/s. Change from resting, in sensor counts. * has no switch fitted. Ctrl-C quits.", ""]
            for bank in range(5):
                names, numbers = [], []
                for i in range(BANK_SIZE):
                    channel = bank * BANK_SIZE + i
                    entry = CHANNELS.get(channel)
                    bare = "*" if channel not in kb.channel_names else ""
                    names.append(f"{(entry[1] if entry else '-')[:4] + bare:>6}")
                    numbers.append(f"{deltas[channel]:+6.0f}")
                lines += ["".join(names), "".join(numbers), ""]
            name = CHANNELS.get(strongest, (0, f"channel {strongest}"))[1]
            lines.append(f"Strongest: {name} {deltas[strongest]:+.0f}. A strong magnet can act like a key press, so keep it away from anything that matters.")
            screen.draw(lines)
            time.sleep(0.03)


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
    if not _in_job():
        Keyboard().close()  # say what's wrong with the keyboard before creating virtual devices
    mapper = Mapper(maps, args.deadzone, args.full, args.curve, args.rapid, args.mouse_speed, args.wheel_speed,
                    args.tap_rate, args.run_rate, not args.no_swallow, args.pause_key)
    read = sorted({*mapper.keys, *(explicit or [])}, key=lambda n: BY_NAME[n][0]) if args.keys != "all" else None
    if mapper.ss_taps and read is not None:
        fixed = read
        read = lambda: fixed if mapper.read_keys() is not None else None  # all keys while SS is down
    for player, pad in mapper.pads.items():
        _say(f"Virtual controller {player} ready: {pad.path}")
    if mapper.pedals:
        _say(f"Virtual pedals ready: {mapper.pedals.path}. They have no default bindings, so bind them in your game once.")
    _drive(mapper, args.hz or 120, read, _map_lines, sinks, explicit, _lights_from(args) if args.lights else None)


def _apply_shortcuts(saved):
    """Run the shortcuts as they now stand, or stop if there are none left."""
    client = daemon_client()
    if client is None:
        return
    try:
        if saved:
            result = _job_call(client, OP_JOB, {"slot": "shortcuts", "label": "shortcuts", "argv": ["shortcuts"], "cwd": os.getcwd()})
            if not result["ok"]:
                raise ApexError(result["log"].strip().replace("apex-analog: ", "", 1) or "It stopped straight away.")
        else:
            _job_call(client, OP_OFF, b"shortcuts")
    finally:
        client.close()


def cmd_shortcut(args):
    """Hold the SteelSeries key and press another key to fire an output."""
    path = _config_path("shortcuts.json")
    saved = _load_json(path)
    if args.action == "list":
        if args.json:
            print(json.dumps(saved))
        else:
            print("\n".join(f"{shortcut_name(key)}: {output}" for key, output in sorted(saved.items()))
                  or "No shortcuts yet. Try: apex-analog shortcut add J volume+")
        return 0
    if not args.key:
        raise ValueError(f"Name the key: apex-analog shortcut {args.action} J")
    key = key_name(args.key)
    if args.action == "add":
        if not args.output:
            raise ValueError("Say what it does, like: apex-analog shortcut add J key:F13, macro:NAME, run:your-command or volume+")
        Mapping(shortcut_rule(key, args.output))  # refuses a bad rule before it's saved
        saved[key] = args.output
    else:
        saved.pop(key, None)
    _write_private(path, saved)
    _apply_shortcuts(saved)
    print(f"{shortcut_name(key)}: {saved[key]}" if key in saved else f"{shortcut_name(key)} removed.")
    if key == SHORTCUT_HOLD and key in saved:
        print("It fires on a quick tap with nothing else pressed, so holding SS still works for its combos. "
              "F9-F12 and the arrows have no sensor, so a quick SS+F10 can look like a tap.")
    return 0


def shortcut_name(key):
    return "SS tap" if key == SHORTCUT_HOLD else f"SS+{key}"


def cmd_shortcuts(args):
    saved = _load_json(_config_path("shortcuts.json"))
    if not saved:
        raise ApexError("No shortcuts yet. Add one with: apex-analog shortcut add J volume+")
    mapper = Mapper([shortcut_rule(key, output) for key, output in saved.items()], swallow=False, pause_key=None)
    _drive(mapper, args.hz or 120, mapper.read_keys if mapper.ss_taps else mapper.keys, _map_lines)


def shortcut_rule(key, output):
    """A saved shortcut as a map rule. The one on SS itself fires on a tap."""
    return f"{SHORTCUT_HOLD}={output}" if key == SHORTCUT_HOLD else f"{SHORTCUT_HOLD}+{key}={output}"


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
    if not _in_job():
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
    if not _in_job():
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
    if args.off or args.on or args.reset:
        client = daemon_client()
        if client is None:
            raise ApexError("Couldn't reach the apex-analog daemon.")
        try:
            client.call(OP_OFF, b"recording")
            if args.reset and os.path.exists(path):
                os.remove(path)
            if not args.off:
                _job_call(client, OP_JOB, {"slot": "recording", "label": "recording", "argv": ["record"]})
        finally:
            client.close()
        print("Cleared. Recording again." if args.reset else "Recording on." if args.on else "Recording off.")
        return 0
    with Keyboard() as kb:
        settings = kb.actuation()
    print("\n".join(TypingProfile(_load_json(path)).report(statistics.median(s["press"] for s in settings.values()))))
    return 0


class TypingRecorder:
    """Adds finished presses to the saved per-key totals, writing them out every so often."""

    def __init__(self, press_points, path=None, every=30.0):
        self.path = path or _state_path("typing-profile.json")
        self.press_points, self.every = press_points, every
        self.profile = TypingProfile(_load_json(self.path))
        self.log = _PressLog()
        self.saved = time.monotonic()
        self.unsaved = False

    def update(self, depths, now):
        for press in self.log.update(depths, now):
            self.profile.add(press, self.press_points.get(press.key))
            self.unsaved = True
        if self.unsaved and now - self.saved >= self.every:
            self.save()
            self.saved = now

    def save(self):
        if self.unsaved:
            _write_private(self.path, {"keys": self.profile.keys})
            self.unsaved = False


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
        deep = kb.deep_actuation()
        for name, value in deep.items():
            settings[name]["deep"] = min(LEVEL_TENTHS, key=lambda t: abs(t / 10 - value["release"])) / 10 if value else None
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


def set_deep(kb, names, mm):
    """Where a deeper press starts, which has to be deeper than where the key first fires. 0 puts it back at the bottom."""
    names = [n for n in names if n != "LGUI"]
    if mm:
        _, press, release = actuation_level(mm)
        first = kb.actuation(names)
        shallow = [n for n in names if first[n]["press_raw"] > press or first[n]["release_raw"] > release]
        if shallow:
            raise ValueError(f"A deeper press has to start past where the key first fires. Too shallow for: {' '.join(shallow)}")
    kb.set_deep_actuation(names, mm or None)
    time.sleep(0.1)
    got = kb.deep_actuation(names)
    wrong = [n for n in names if (got[n] is None) != (not mm) or (mm and (got[n]["press_raw"], got[n]["release_raw"]) != (press, release))]
    if wrong:
        raise ApexError(f"Sent, but these read back differently: {' '.join(wrong)}")
    return names


def cmd_actuation(args):
    if args.mm is None and args.keys is not None:
        raise ValueError("Say how deep too, like: apex-analog actuation W 1.0")
    with Keyboard() as kb:
        if args.deep and args.keys is not None:
            names = list(kb.keys) if args.keys == "all" else key_names(args.keys)
            missing = [n for n in names if n not in kb.keys]
            if missing:
                raise ValueError(f"No sensor fitted on this keyboard for: {' '.join(missing)}")
            done = set_deep(kb, names, args.mm)
            count = f"{len(done)} key{'s' if len(done) != 1 else ''}"
            print(f"The deeper press on {count} starts at {actuation_level(args.mm)[0]:.1f} mm, read back from the keyboard. "
                  f"Give it something to do with apex-analog bind KEY ACTION --layer deep." if args.mm else
                  f"The deeper press on {count} is back at the very bottom.")
            if "LGUI" in names:
                print("LGUI skipped: it can't have a deeper press.")
            return 0
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
        deep = kb.deep_actuation()
    groups = {}
    for name, value in settings.items():
        groups.setdefault(value["press"], []).append(name)
    for press, names in sorted(groups.items()):
        print(f"{press:.2f} mm: " + ("every other key" if len(names) > 30 else " ".join(names)))
    deeper = {}
    for name, value in deep.items():
        if value:  # named by its level, which the release point sits closest to
            level = min(LEVEL_TENTHS, key=lambda t: abs(t / 10 - value["release"])) / 10
            deeper.setdefault(level, []).append(name)
    for level, names in sorted(deeper.items()):
        print(f"Deeper press at {level:.1f} mm: " + " ".join(names))
    return 0


def _add_light_options(parser):
    parser.add_argument("--effect", default="glow", choices=EFFECTS, help="glow (default), ripple, music, stats, rev or heatmap")
    parser.add_argument("--stats", default="cpu,ram", help=f"for --effect stats: up to {len(STAT_ROWS)} of {','.join(STAT_NAMES)}, starting from the F row (default cpu,ram)")
    parser.add_argument("--port", type=int, default=4444, help="for --effect rev: the UDP port set in BeamNG's OutGauge options (default 4444)")
    parser.add_argument("--redline", type=float, metavar="RPM", help="for --effect rev: the bar's full scale. Otherwise it learns the highest RPM it sees")
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
            "Desktop: volume+ volume- brightness+ brightness-. Any command: run:your-command. A saved macro: macro:NAME. "
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
    ac.add_argument("--deep", action="store_true",
                    help="set where a deeper press starts instead, for bindings with --layer deep. 0 puts it back at the bottom")
    ac.set_defaults(func=cmd_actuation)

    pa = commands.add_parser("paint", help="set key colours from any program, one JSON line at a time on stdin")
    pa.add_argument("--stay", action="store_true", help="leave the colours on at the end instead of restoring your normal lighting")
    pa.set_defaults(func=cmd_paint)

    ra = commands.add_parser("rapid", help="see or set the keyboard's own rapid tap, per key")
    ra.add_argument("keys", nargs="?", help="W, WASD, W,A,SPACE, or all. Leave out to just see which keys have it on")
    ra.add_argument("state", nargs="?", choices=("on", "off"))
    ra.set_defaults(func=cmd_rapid)

    bi = commands.add_parser("bindings", help="what every key does, set on the keyboard itself")
    bi.add_argument("--layer", choices=("base", "ss", "deep", "all"), default="all",
                    help="base: normally, ss: while the SteelSeries key is held, deep: on a deeper press, all (default)")
    bi.add_argument("--full", action="store_true", help="list every key, not just the ones that do something special")
    bi.add_argument("--json", action="store_true")
    bi.set_defaults(func=cmd_bindings)

    bd = commands.add_parser("bind", help="change what a key does, on the keyboard itself, so it works anywhere",
                             description="Change what a key does, on the keyboard itself, so it works with no software running.",
                             epilog="Actions: a key or up to 4 at once (esc, f13, ctrl+c, alt+tab, kp-enter), media:play-pause, media:next, "
                                    "media:previous, media:stop, media:mute, media:volume-up, media:volume-down, mouse1 to mouse8, "
                                    "scroll-up, scroll-down, tilt-left, tilt-right, none, or default to put it back. "
                                    "It lasts until you unplug the keyboard. apex-analog save keeps it.")
    bd.add_argument("key", help="CAPS, F1, KP_ENTER, WHEEL_PRESS, MEDIA_BUTTON, TILT_LEFT, TILT_RIGHT, WHEEL_UP, WHEEL_DOWN, or any other key")
    bd.add_argument("action", help="esc, ctrl+c, media:mute, mouse4, none, default...")
    bd.add_argument("--layer", choices=("base", "ss", "deep"), default="base",
                    help="base: normally (default), ss: while the SteelSeries key is held, deep: on a deeper press")
    bd.set_defaults(func=cmd_bind)

    lk = commands.add_parser("layer-key", help="which key turns on the SteelSeries layer while held, so the SteelSeries key itself can be bound")
    lk.add_argument("key", nargs="?", help="any key, or SS to put it back. Leave out to see which it is")
    lk.set_defaults(func=cmd_layer_key)

    pr = commands.add_parser("preset", help="keep named actuation and rapid tap setups on this computer")
    pr.add_argument("action", choices=("save", "use", "list", "delete"))
    pr.add_argument("name", nargs="?")
    pr.set_defaults(func=cmd_preset)

    sv = commands.add_parser("save", help="write the current settings to the keyboard's flash, so they survive a replug")
    sv.add_argument("--slot", type=int, help="saved config 1 to 5 (default: the active one)")
    sv.add_argument("--yes", action="store_true", help="don't ask first")
    sv.set_defaults(func=cmd_save)

    au = commands.add_parser("auto", help="switch presets and lighting by the focused window (KDE, needs kdotool)")
    au.add_argument("--config", help="rules file (default ~/.config/apex-analog/auto.json)")
    au.add_argument("--example", action="store_true", help="print an example rules file")
    au.add_argument("--hz", type=float, help="updates per second")
    au.set_defaults(func=cmd_auto)

    dm = commands.add_parser("daemon", help="the background process every command goes through. It starts by itself")
    dm.set_defaults(func=cmd_daemon)

    rc = commands.add_parser("record")  # the job behind profile
    rc.add_argument("--hz", type=float)
    rc.set_defaults(func=cmd_record)

    sa = commands.add_parser("status", help="see what's on")
    sa.set_defaults(func=cmd_status)

    of = commands.add_parser("off", help="turn one thing off: gamepad, lights, overlay, oled, stream, recording, or all")
    of.add_argument("name")
    of.set_defaults(func=cmd_off)

    hp = commands.add_parser("help", help="everything it can do, or the details of one command")
    hp.add_argument("topic", nargs="?")
    hp.set_defaults(func=cmd_help)

    st = commands.add_parser("autostart", help="start the daemon at login, which brings back your lights and recording")
    st.add_argument("action", choices=("on", "off", "status"))
    st.add_argument("command_args", nargs=argparse.REMAINDER, metavar="...", help="what to run, like: lights --effect ripple")
    st.set_defaults(func=cmd_autostart)

    sp = commands.add_parser("stop", help="stop the daemon and everything it runs")
    sp.set_defaults(func=cmd_stop)

    mc = commands.add_parser("macro", help="save, record and run keystroke sequences, and fire them from any key with macro:NAME")
    mc.add_argument("action", choices=("add", "record", "run", "list", "delete"))
    mc.add_argument("name", nargs="?")
    mc.add_argument("steps", nargs="*", metavar="STEP", help='for add: a combo like LCTRL+C, a wait in ms like 100, or text:"hello there"')
    mc.set_defaults(func=cmd_macro)

    ol = commands.add_parser("oled", help="show key depth on the keyboard's own screen, through apex-oledd")
    ol.add_argument("--map", action="store_true", help="a live pressure map of every key, instead of the deepest key's depth")
    ol.add_argument("--hz", type=float, help="updates per second")
    ol.set_defaults(func=cmd_oled)

    ma = commands.add_parser("magnet", help="watch every sensor react to a magnet, including the ones with no switch")
    ma.set_defaults(func=cmd_magnet)

    tp = commands.add_parser("typeprint", help="experiment: recognise you by how you press keys. Not security")
    tp.add_argument("action", choices=("enroll", "check", "forget"), help="enroll: type a set phrase 8 times. check: try to pass. forget: delete it")
    tp.set_defaults(func=cmd_typeprint)

    pf = commands.add_parser("profile", help="how you type, as per-key totals only, and what that means for actuation. Records all the time")
    pf.add_argument("--off", action="store_true", help="stop recording")
    pf.add_argument("--on", action="store_true", help="start recording again")
    pf.add_argument("--reset", action="store_true", help="delete what's recorded and start over")
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

    sc = commands.add_parser("shortcut", help="hold the SteelSeries key and press another to fire something")
    sc.add_argument("action", choices=("add", "remove", "list"))
    sc.add_argument("key", nargs="?")
    sc.add_argument("output", nargs="?", help="key:F13, macro:NAME, run:your-command, volume+, or anything else map takes")
    sc.add_argument("--json", action="store_true")
    sc.set_defaults(func=cmd_shortcut)

    ss = commands.add_parser("shortcuts", help="run your saved shortcuts. shortcut add starts this for you")
    ss.add_argument("--hz", type=float, help="updates per second")
    ss.set_defaults(func=cmd_shortcuts)

    i = commands.add_parser("info", help="the keyboard, your access, and every key's actuation settings")
    i.add_argument("--json", action="store_true")
    i.set_defaults(func=cmd_info)
    for name, sub in commands.choices.items():
        if name in SLOTS:
            sub.add_argument("--watch", action="store_true", help="stay in this terminal, showing what it's doing, instead of running in the background")
    parser.command_parsers = commands.choices
    return parser


NEEDS_NO_DAEMON = {"daemon", "stop", "status", "off", "autostart", "macro", "help"}


def _leave_with_the_daemon():
    """A job ends cleanly if the daemon that started it goes away for any reason."""
    parent = os.getppid()

    def watch():
        while os.getppid() == parent:
            time.sleep(2)
        os.kill(os.getpid(), signal.SIGTERM)
    threading.Thread(target=watch, daemon=True).start()


def _runs_as_a_job(args):
    if args.command not in SLOTS or _in_job() or getattr(args, "watch", False):
        return False
    if args.command == "stream" and not (args.udp or args.osc or args.serve):
        return False  # its output is the point: it prints to this terminal or pipe
    if args.command == "lights" and args.off:
        return False
    return True


def main(argv=None):
    raw = list(sys.argv[1:] if argv is None else argv)
    args = build_parser().parse_args(raw)
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    if _in_job():
        _leave_with_the_daemon()
    try:
        if _runs_as_a_job(args):
            return _detach(args, raw)
        if args.command not in NEEDS_NO_DAEMON and not _in_job():
            client = daemon_client()  # start it if needed, so this and everything after it go through one owner
            if client:
                client.close()
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
