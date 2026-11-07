#!/usr/bin/env python3
"""python3 -m unittest    No keyboard needed. Virtual device tests hold their own output so nothing reaches your desktop."""
import contextlib
import io
import json
import fcntl
import math
import os
import re
import random
import select
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
import unittest

import apex_analog as a

HAVE_UINPUT = os.access("/dev/uinput", os.W_OK)
EVENT = "llHHi"


def hold(path):
    """Open a virtual device exclusively, so only this test sees its events."""
    for _ in range(100):
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
            fcntl.ioctl(fd, a.EVIOCGRAB, 1)
            return fd
        except OSError:
            time.sleep(0.02)
    raise OSError(f"could not open {path}")


def drain(fd, wait=0.2):
    out, size = [], struct.calcsize(EVENT)
    while select.select([fd], [], [], wait)[0]:
        data = os.read(fd, size * 64)
        for offset in range(0, len(data), size):
            _, _, kind, code, value = struct.unpack_from(EVENT, data, offset)
            if kind != a.EV_SYN:
                out.append((kind, code, value))
        wait = 0.05
    return out


class Curve(unittest.TestCase):
    def test_firmware_points(self):
        self.assertEqual(a.curve_mm(0), 0.0)
        self.assertAlmostEqual(a.curve_mm(128), 3.0)
        self.assertAlmostEqual(a.curve_mm(215), 4.0)
        self.assertEqual(a.curve_mm(255), 4.0)

    def test_never_goes_backward(self):
        values = [a.curve_mm(n) for n in range(256)]
        self.assertEqual(values, sorted(values))


class Names(unittest.TestCase):
    def test_forms(self):
        self.assertEqual(a.key_names("wasd"), ["W", "A", "S", "D"])
        self.assertEqual(a.key_names("W,space,comma"), ["W", "SPACE", ","])
        self.assertEqual(a.key_names("shift"), ["LSHIFT"])
        self.assertEqual(a.key_names(["w", ";"]), ["W", ";"])
        self.assertIsNone(a.key_names(None))

    def test_key_without_sensor_says_which_have_one(self):
        with self.assertRaisesRegex(ValueError, "SPACE"):
            a.key_name("ESC")

    def test_every_channel_fits_a_bank(self):
        self.assertTrue(all(0 <= c < 5 * a.BANK_SIZE for c in a.CHANNELS))
        self.assertEqual(len({h for h, _, _ in a.CHANNELS.values()}), len(a.CHANNELS))


class Safety(unittest.TestCase):
    def test_only_read_commands_can_be_sent(self):
        kb = a.Keyboard.__new__(a.Keyboard)
        kb.fd = None
        for command in (0x01, 0x81, 0x58, 0x59, 0x5A, 0x74, 0x7F, 0x69, 0x61, 0x65):
            with self.assertRaisesRegex(ValueError, "refused"):
                kb._ask([command])

    def test_allowlist_has_not_grown_by_accident(self):
        self.assertEqual(set(a.ALLOWED), {0xD7, 0xDA, 0xB1, 0xB2, 0x9E, 0xE2, 0xE0, 0xE3, 0xE4, 0xB3, 0xCD})
        self.assertEqual(set(a.BIND_COMMANDS), {0x63, 0x64, 0x60, 0x4D})
        self.assertEqual(set(a.BIND_CHECKS), set(a.BIND_COMMANDS))

    def test_only_known_write_commands_can_be_sent(self):
        kb = a.Keyboard.__new__(a.Keyboard)
        kb.fd = None
        known = {*a.LIGHT_COMMANDS, *a.SETTING_COMMANDS, a.SAVE_COMMAND, *a.BIND_COMMANDS}
        for command in range(256):
            if command not in known:
                with self.assertRaisesRegex(ValueError, "refused"):
                    kb._write([command])

    def test_stray_report_is_not_taken_for_sensor_data(self):
        self.assertFalse(a._valid_bank(bytes([7, 1]) + bytes(62)))
        self.assertFalse(a._valid_bank(struct.pack("<28H", *[5000] * 28)))
        self.assertTrue(a._valid_bank(struct.pack("<28H", *[850] * 28) + bytes(8)))

    def test_network_needs_chosen_keys(self):
        def run(*argv):
            sinks = a._network_sinks(a.build_parser().parse_args(["stream", *argv]))
            for sink in sinks:
                sink.socket.close()
            return sinks
        with self.assertRaisesRegex(ValueError, "--keys"):
            run("--udp", "127.0.0.1:9000")
        with self.assertRaisesRegex(ValueError, "passwords"):
            run("--udp", "203.0.113.5:9000", "--keys", "all")
        self.assertEqual(len(run("--udp", "127.0.0.1:9000", "--keys", "all")), 1)
        self.assertEqual(len(run("--udp", "203.0.113.5:9000", "--keys", "WASD")), 1)

    def test_web_pages_cannot_connect(self):
        self.assertTrue(a.origin_allowed(None))
        self.assertTrue(a.origin_allowed("http://localhost:3000"))
        self.assertTrue(a.origin_allowed("http://127.0.0.1:8000"))
        self.assertFalse(a.origin_allowed("https://evil.example"))
        self.assertFalse(a.origin_allowed("https://localhost.evil.example"))
        self.assertFalse(a.origin_allowed("null"))


class SimulatedKeyboard:
    """Answers the binding commands the way firmware 4.16.8 does, quirks included, standing in for the daemon. Anything else fails."""
    # Keys the firmware's table can bind that apex-analog never names: they aren't on this board, or FB shares ENTER's entry.
    UNNAMED = (0xF2, 0xF3, 0xF4, 0xF6, 0xF7, 0xF8, 0xFB)

    def __init__(self):
        hids = [*a.BIND_NAMES, *self.UNNAMED]
        self.tables = {layer: {hid: a.default_binding(layer, hid) for hid in hids} for layer in (0, 1)}
        self.tables[2] = {hid: bytes(5) for hid in a.DEEP_HIDS}
        self.wheel = {layer: list(records) for layer, records in a.DEFAULT_WHEEL.items()}
        self.layer_key = 0xF0
        self.first = {hid: (50, 46) for hid in a.DEEP_HIDS}  # press and release points, n/255
        self.second = {hid: (255, 255) for hid in a.DEEP_HIDS}  # where a deeper press starts: the bottom, out of the box
        self.lights = bytearray(32)  # keys lit while the SteelSeries layer is on, one bit per HID usage
        for hid, record in self.tables[1].items():
            if record[0]:
                self.lights[hid // 8] |= 1 << (hid % 8)
        self.sent = []

    def close(self):
        pass

    def call(self, op, data=b""):
        payload = bytes(data)
        self.sent.append(payload)
        if op == a.OP_ASK:
            return self.ask(payload)
        if op == a.OP_WRITE:
            self.write(payload)
            return b""
        raise AssertionError(f"unexpected op {op}")

    def table(self, layer_byte):
        return 1 if layer_byte == 1 else 2 if layer_byte == 2 else 0  # any other byte lands on the normal layer

    def ask(self, p):
        if p[0] == 0xE0:
            return bytes([self.layer_key]).ljust(64, b"\0")
        if p[0] == 0xCD:
            return bytes(self.lights).ljust(64, b"\0")
        if p[0] in (0xB1, 0xB2, 0xB3):
            deep = p[0] == 0xB3 and p[1] == 2
            count, first, stride = (p[2], 3, 3) if p[0] == 0xB3 else (p[1], 2, 3 if p[0] == 0xB1 else 2)
            out = b""
            for i in range(count):
                hid = p[first + stride * i]
                if p[0] == 0xB2:
                    out += bytes([hid, 0])
                else:
                    out += bytes([hid, *(self.second if deep else self.first)[hid]])
            return out.ljust(64, b"\0")
        if p[0] == 0xE4:
            return b"".join(self.wheel[0 if p[1] == 0 else 1]).ljust(64, b"\0")
        if p[0] == 0xE3:
            table, count = self.tables[self.table(p[1])], p[2]
            assert 1 <= count <= 10, p.hex()
            out, request = b"", 3
            for _ in range(count):
                hid = p[request]
                if self.table(p[1]) == 2 and hid not in a.DEEP_HIDS:
                    raise AssertionError(f"asked layer 2 about {hid:#04x}: the firmware's reply would go out of step")
                out += bytes([hid]) + table.get(hid, b"\xee" * 5)
                request += 6
            return out.ljust(64, b"\0")
        raise AssertionError(f"unexpected read {p.hex(' ')}")

    def write(self, p):
        if p[0] == 0x63:
            layer = self.table(p[1])
            for i in range(p[2]):
                hid, record = p[3 + 6 * i], p[4 + 6 * i:9 + 6 * i]
                if layer == 2 and (self.first[hid][0] > self.second[hid][0] or self.first[hid][1] > self.second[hid][1]):
                    continue  # refused: the deeper press would start before the first one
                self.tables[layer][hid] = bytes(record)
        elif p[0] == 0x64:
            self.wheel[0 if p[1] == 0 else 1] = [bytes(p[2:7]), bytes(p[7:12])]
        elif p[0] == 0x60:
            self.layer_key = p[1]
        elif p[0] == 0x4D:
            assert len(p) == 33, p.hex()  # the firmware ignores a shorter one
            self.lights = bytearray(p[1:33])
        elif p[0] == 0x33:
            assert p[1] == 2, p.hex()
            for i in range(p[2]):
                hid, press, release = p[3 + 3 * i:6 + 3 * i]
                if press >= self.first[hid][0] and release >= self.first[hid][1]:
                    self.second[hid] = (press, release)
        else:
            raise AssertionError(f"unexpected write {p.hex(' ')}")


RealKeyboard = a.Keyboard


def simulated(sim=None):
    kb = RealKeyboard.__new__(RealKeyboard)
    kb.fd, kb.client = None, sim or SimulatedKeyboard()
    kb.keys = [name for name, (_, hid, _) in a.BY_NAME.items() if not name.startswith("INT") and name not in ("#", "ISO\\")]
    return kb


class Bindings(unittest.TestCase):
    def test_ss_layer_binds_light_up_and_unbinds_go_dark(self):
        sim = SimulatedKeyboard()
        kb = simulated(sim)
        comma, dot = a.BIND_HIDS[a.bind_key(",")], a.BIND_HIDS[a.bind_key(".")]
        self.assertNotIn(comma, kb.layer_lights())
        kb.set_bindings(1, {",": a.parse_binding("media:previous"), ".": a.parse_binding("media:next")})
        self.assertTrue({comma, dot} <= kb.layer_lights())
        kb.set_bindings(1, {",": a.default_binding(1, comma)})
        self.assertNotIn(comma, kb.layer_lights())
        self.assertIn(dot, kb.layer_lights())
        lit_before = kb.layer_lights()
        kb.set_bindings(0, {"CAPS": a.parse_binding("esc")})  # the normal layer leaves the lights alone
        self.assertEqual(kb.layer_lights(), lit_before)

    def test_layer_light_writes_are_checked(self):
        with self.assertRaises(ValueError):
            a._check_layer_lights([0x4D] + [0] * 31)
        a._check_layer_lights([0x4D] + [0] * 32)
        self.assertIn(0x4D, a.BIND_COMMANDS)
        self.assertIn(0xCD, a.ALLOWED)

    def test_actions_round_trip(self):
        for text in ("esc", "ctrl+c", "ctrl+shift+z", "alt+tab", "f13", "kp-enter", "numlock", "media:mute", "media:volume-up",
                     "mouse4", "scroll-up", "tilt-left", "none", "builtin:10", "builtin:2:1", "int3", "iso\\"):
            record = a.parse_binding(text)
            a._check_record(record)
            self.assertEqual(a.describe_binding(record), text)
        self.assertEqual(a.parse_binding("LCTRL+BKSP"), a.parse_binding("ctrl+backspace"))
        self.assertEqual(a.parse_binding("lgui"), bytes([0x51, 0xE3, 0, 0, 0]))
        self.assertEqual(a.parse_binding("caps"), bytes([0x51, 0x39, 0, 0, 0]))
        for text in ("dpi", "builtin:9", "media:louder", "a+b+c+d+e", "nope"):
            with self.assertRaises(ValueError):
                a.parse_binding(text)

    def test_same_actions_as_the_mouse(self):
        try:
            sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "aerox"))
            import aerox
        except ImportError:
            self.skipTest("aerox isn't next to apex-analog")
        finally:
            sys.path.pop(0)
        for text in ("alt+tab", "ctrl+shift+z", "meta", "printscreen", "f24", "mouse8", "media:play-pause", "scroll-down", "tilt-right", "none"):
            self.assertEqual(a.parse_binding(text), aerox.parse_action(text), text)
            self.assertEqual(a.describe_binding(a.parse_binding(text)), aerox.describe_action(aerox.parse_action(text)), text)
        for usage, name in aerox.KEYS.items():
            self.assertEqual(a.OUTPUT_KEYS[usage], name)

    def test_names_cover_the_firmware_table_and_nothing_else(self):
        self.assertEqual(len(a.BIND_HIDS), 114)
        self.assertEqual(len(set(a.BIND_HIDS.values())), 114)
        self.assertTrue(a.DEEP_HIDS <= set(a.BIND_NAMES))
        self.assertNotIn(0xF1, a.DEEP_HIDS)
        self.assertNotIn(0x65, a.BIND_NAMES)  # no Menu key in the firmware's table

    def test_only_known_records_are_sent(self):
        ok = [0x63, 0, 1, a.BIND_HIDS["CAPS"], *a.parse_binding("esc")]
        a._check_bind(ok)
        a._check_bind([0x63, 0, 1, 0xF0, 0x51, 0xF0, 0, 0, 0])  # SS's own default can always be put back
        bad = {
            "layer": [0x63, 3, 1, 0x39, 0x51, 0x29, 0, 0, 0],
            "count": [0x63, 0, 0],
            "too many": [0x63, 0, 11] + [0x39, 0x51, 0x29, 0, 0, 0] * 11,
            "length": [0x63, 0, 2, 0x39, 0x51, 0x29, 0, 0, 0],
            "unnamed key": [0x63, 0, 1, 0xF2, 0x51, 0x29, 0, 0, 0],
            "no sensor": [0x63, 2, 1, 0x3A, 0x51, 0x29, 0, 0, 0],
            "macro": [0x63, 0, 1, 0x39, 0x71, 0, 0, 1, 0],
            "unknown flag": [0x63, 0, 1, 0x39, 0x30, 0, 0, 0, 0],
            "buffer": [0x63, 0, 1, 0x39, 0x72, 5, 0, 0, 0],
            "bitmap": [0x63, 0, 1, 0x39, 0x73, 1, 0, 0, 0],
            "stray byte": [0x63, 0, 1, 0x39, 0x61, 0xE2, 0, 0, 7],
            "unknown usage": [0x63, 0, 1, 0x39, 0x51, 0xF0, 0, 0, 0],
            "made-up function": [0x63, 1, 1, 0x39, 0x62, 9, 0, 0, 0],
            "empty keys": [0x63, 0, 1, 0x39, 0x51, 0, 0, 0, 0],
        }
        for why, payload in bad.items():
            with self.assertRaisesRegex(ValueError, "refused", msg=why):
                a._check_bind(payload)
        a._check_wheel([0x64, 0, *a.parse_binding("media:next"), *a.parse_binding("media:previous")])
        for payload in ([0x64, 2] + [0] * 10, [0x64, 0] + [0] * 9, [0x64, 1, 0x71, 0, 0, 1, 0] + [0] * 5):
            with self.assertRaisesRegex(ValueError, "refused"):
                a._check_wheel(payload)

    def test_reads_every_layer_in_tens(self):
        sim = SimulatedKeyboard()
        kb = simulated(sim)
        base = kb.bindings(0)
        self.assertEqual(len(base), 114)
        self.assertEqual(base["CAPS"], bytes([0x51, 0x39, 0, 0, 0]))
        self.assertEqual(a.describe_binding(base["WHEEL_PRESS"]), "media:mute")
        self.assertEqual(a.describe_binding(kb.bindings(1)["Q"]), "builtin:10")
        self.assertEqual(len(kb.bindings(2)), len(a.DEEP_HIDS))
        self.assertTrue(all(p[2] <= 10 for p in sim.sent if p[0] == 0xE3))
        with self.assertRaisesRegex(ValueError, "deeper press"):
            kb.bindings(2, ["F1"])
        self.assertEqual(kb.wheel(0), a.DEFAULT_WHEEL[0])
        self.assertEqual(kb.layer_key(), 0xF0)

    def test_report_marks_what_changed(self):
        sim = SimulatedKeyboard()
        sim.tables[0][0x39] = a.parse_binding("esc")
        report = a.read_bindings(simulated(sim))
        self.assertEqual(report["layer_key"], "SS")
        self.assertEqual(report["layers"]["base"]["CAPS"], {"action": "esc", "raw": "5129000000", "default": False})
        self.assertTrue(report["layers"]["base"]["A"]["default"])
        self.assertEqual(report["wheel"]["base"]["WHEEL_UP"]["action"], "media:volume-up")
        self.assertEqual(set(report["layers"]), {"base", "ss", "deep"})
        self.assertEqual(set(report["wheel"]), {"base", "ss"})

    def run_cli(self, sim, *argv):
        a.Keyboard = lambda *_, **__: simulated(sim)
        self.addCleanup(setattr, a, "Keyboard", RealKeyboard)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            args = a.build_parser().parse_args(list(argv))
            args.func(args)
        return out.getvalue()

    def test_bind_and_put_back(self):
        sim = SimulatedKeyboard()
        said = self.run_cli(sim, "bind", "caps", "esc")
        self.assertIn("CAPS now does esc normally", said)
        self.assertEqual(sim.tables[0][0x39], a.parse_binding("esc"))
        self.assertIn(bytes([0x63, 0, 1, 0x39, 0x51, 0x29, 0, 0, 0]), sim.sent)
        self.run_cli(sim, "bind", "CAPS", "default")
        self.assertEqual(sim.tables[0][0x39], bytes([0x51, 0x39, 0, 0, 0]))
        self.run_cli(sim, "bind", "J", "media:play-pause", "--layer", "ss")
        self.assertEqual(sim.tables[1][0x0D], a.parse_binding("media:play-pause"))
        self.run_cli(sim, "bind", "WHEEL_DOWN", "media:next")
        self.assertEqual(sim.wheel[0], [a.DEFAULT_WHEEL[0][0], a.parse_binding("media:next")])
        self.run_cli(sim, "bind", "W", "e", "--layer", "deep")
        self.assertEqual(sim.tables[2][0x1A], a.parse_binding("e"))
        listing = self.run_cli(sim, "bindings")
        self.assertRegex(listing, r"\n  J +media:play-pause +\(changed\)")
        self.assertRegex(listing, r"\n  WHEEL_DOWN +media:next +\(changed\)")
        self.assertRegex(listing, r"\n  Q +builtin:10\n")

    def test_move_the_layer_then_bind_the_steelseries_key(self):
        sim = SimulatedKeyboard()
        self.assertIn("Holding SS turns on", self.run_cli(sim, "layer-key"))
        self.assertIn("Holding SCRLK now turns on", self.run_cli(sim, "layer-key", "scrlk"))
        self.assertEqual(sim.layer_key, 0x47)
        self.run_cli(sim, "bind", "SS", "media:play-pause")
        self.assertEqual(sim.tables[0][0xF0], a.parse_binding("media:play-pause"))
        with self.assertRaisesRegex(ValueError, "never fires"):
            self.run_cli(sim, "bind", "SCRLK", "f13")
        self.assertIn("back on the SteelSeries key", self.run_cli(sim, "layer-key", "SS"))
        self.assertEqual(sim.layer_key, 0xF0)
        with self.assertRaisesRegex(ValueError, "not a control"):
            self.run_cli(sim, "layer-key", "WHEEL_PRESS")
        for payload in ([0x60], [0x60, 0xF2], [0x60, 0x47, 0]):
            with self.assertRaisesRegex(ValueError, "refused"):
                a._check_layer_key(payload)

    def test_deeper_press(self):
        sim = SimulatedKeyboard()
        said = self.run_cli(sim, "bind", "W", "e", "--layer", "deep")
        self.assertIn("still at the very bottom", said)
        said = self.run_cli(sim, "actuation", "W,A", "3.0", "--deep")
        self.assertIn("starts at 3.0 mm", said)
        _, press, release = a.actuation_level(3.0)
        self.assertEqual(sim.second[0x1A], (press, release))
        self.assertIn(bytes([0x33, 2, 2, 0x1A, press, release, 0x04, press, release]), sim.sent)
        self.assertNotIn("still at the very bottom", self.run_cli(sim, "bind", "W", "e", "--layer", "deep"))
        self.assertIn("Deeper press at 3.0", self.run_cli(sim, "actuation"))
        with self.assertRaisesRegex(ValueError, "past where the key first fires"):
            self.run_cli(sim, "actuation", "S", "0.4", "--deep")
        self.assertIn("back at the very bottom", self.run_cli(sim, "actuation", "W", "0", "--deep"))
        self.assertEqual(sim.second[0x1A], (255, 255))
        for payload in ([0x33, 0, 1, 0x1A, 9, 9], [0x33, 2, 1, 0x3A, 99, 90], [0x33, 2, 1, 0xE3, 99, 90], [0x33, 2, 1, 0x1A, 90, 99],
                        [0x33, 2, 1, 0x1A, 0, 0], [0x33, 2, 21] + [0x1A, 99, 90] * 21):
            with self.assertRaisesRegex(ValueError, "refused"):
                a._check_deep_actuation(payload)

    def test_refusals_are_explained(self):
        sim = SimulatedKeyboard()
        sim.second[0x04] = (10, 10)  # shallower than where A first fires
        with self.assertRaisesRegex(a.ApexError, "starts shallower"):
            self.run_cli(sim, "bind", "A", "b", "--layer", "deep")
        with self.assertRaisesRegex(ValueError, "never fires"):
            self.run_cli(sim, "bind", "SS", "f13")
        with self.assertRaisesRegex(ValueError, "no sensor"):
            self.run_cli(sim, "bind", "F5", "b", "--layer", "deep")
        with self.assertRaisesRegex(ValueError, "no deeper press"):
            self.run_cli(sim, "bind", "WHEEL_UP", "b", "--layer", "deep")
        self.assertFalse([p for p in sim.sent if p[0] == 0x63 and p[1] == 2 and p[3] != 0x04])


class Firmware(unittest.TestCase):
    def setUp(self):
        original = a._find_control_node
        self.addCleanup(setattr, a, "_find_control_node", original)

    def test_refuses_any_other_firmware(self):
        a._find_control_node = lambda: ("/dev/null", "0417", "1640")
        with self.assertRaisesRegex(a.ApexError, "Refusing"):
            a.Keyboard(direct=True)

    def test_apex_pro_and_tkl_changes_are_experimental(self):
        for product, name in (("1610", "Apex Pro"), ("1614", "Apex Pro TKL")):
            kb = a.Keyboard.__new__(a.Keyboard)
            kb.fd, kb.client = None, SimulatedKeyboard()
            kb._set_model(product)
            self.assertEqual((kb.model, kb.changes, kb.writable), (name, a.EXPERIMENTAL, True))
            a.Keyboard.warned = False
            said = io.StringIO()
            with contextlib.redirect_stderr(said):
                kb._write([0x63, 0, 1, 0x39, 0x51, 0x29, 0, 0, 0])
                kb._write([0x63, 0, 1, 0x39, 0x51, 0x39, 0, 0, 0])
            self.assertEqual(said.getvalue().count("experimental"), 1)
            self.assertIn(name, said.getvalue())
        kb._set_model("1640")
        self.assertEqual(kb.changes, a.TESTED)
        kb._set_model("9999")
        with self.assertRaisesRegex(a.ApexError, "isn't on the"):
            kb._write([0x3B])

    def test_missing_keyboard_is_explained(self):
        a._find_control_node = lambda: (None, None, None)
        with self.assertRaisesRegex(a.NotFound, "plugged in|bootloader"):
            a.Keyboard(direct=True)


class Digital(unittest.TestCase):
    def test_threshold_with_hysteresis(self):
        d = a.Digital(at=2.0)
        self.assertEqual([d.update(m) for m in (0.0, 1.9, 2.0, 1.9, 1.8, 2.0)], [False, False, True, True, False, True])

    def test_rapid_retriggers_without_leaving_the_bottom(self):
        d = a.Digital(at=1.0, rapid=0.3)
        self.assertEqual([d.update(m) for m in (0.0, 2.0, 3.0, 2.6, 2.8, 3.0)], [False, True, True, False, False, True])


class Rules(unittest.TestCase):
    def test_outputs(self):
        self.assertEqual(a.Mapping("W=rt").kind, "trigger")
        m = a.Mapping("a=lx-")
        self.assertEqual((m.key, m.kind, m.target, m.sign), ("A", "stick", "lx", -1))
        m = a.Mapping("J=key:F13@2.5")
        self.assertEqual((m.kind, m.target, m.digital.at), ("key", 183, 2.5))
        self.assertEqual(a.Mapping("U=wheel+").target, "wheel")
        self.assertEqual(a.Mapping("H=mouse-left").target, "left")
        self.assertEqual(a.Mapping("Q=dpad-up").target, "dpad_up")
        self.assertEqual(a.Mapping("==lx+").key, "=")

    def test_bad_rules_explain_themselves(self):
        for bad, why in (("W", "should look like"), ("W=nope", "unknown output"), ("ESC=rt", "no sensor"), ("W=key:NOPE", "unknown key")):
            with self.assertRaisesRegex(ValueError, why):
                a.Mapping(bad)

    def test_presets(self):
        self.assertEqual(a.expand_maps(["drive", "Q=a"]), ["W=rt", "S=lt", "A=lx-", "D=lx+", "Q=a"])


class Wire(unittest.TestCase):
    def test_websocket_accept_matches_rfc6455(self):
        self.assertEqual(a.ws_accept_value("dGhlIHNhbXBsZSBub25jZQ=="), "s3pPLMBiTxaQ9kYGzzhZRbK+xOo=")

    def test_osc_message(self):
        self.assertEqual(a.osc_message("/apex/W", 1.5), b"/apex/W\0,f\0\0" + struct.pack(">f", 1.5))
        self.assertEqual(len(a.osc_message("/abc", 1.0)) % 4, 0)


@unittest.skipUnless(HAVE_UINPUT, "no /dev/uinput access")
class VirtualDevices(unittest.TestCase):
    def mapper(self, *rules, **options):
        mapper = a.Mapper(list(rules), swallow=False, **options)
        self.addCleanup(mapper.close)
        return mapper

    def test_gamepad_axes_and_centering(self):
        with a.Gamepad() as pad:
            fd = hold(pad.path)
            pad.set(lx=-0.5, rt=1.0, a=True, dpad_up=True)
            self.assertEqual(set(drain(fd)), {(a.EV_ABS, 0x00, -16383), (a.EV_ABS, 0x05, 1023), (a.EV_KEY, 0x130, 1), (a.EV_ABS, 0x11, -1)})
            pad.center()
            self.assertEqual(set(drain(fd)), {(a.EV_ABS, 0x00, 0), (a.EV_ABS, 0x05, 0), (a.EV_KEY, 0x130, 0), (a.EV_ABS, 0x11, 0)})
            os.close(fd)

    def test_opposite_keys_cancel_and_deadzone_holds(self):
        m = self.mapper("drive")
        fd = hold(m.pad.path)
        m.update({"A": 0.2}, 0.01)
        self.assertEqual(drain(fd, 0.05), [])
        m.update({"A": 3.5, "D": 3.5, "W": 1.9}, 0.01)
        self.assertEqual(m.values["lx"], 0.0)
        self.assertAlmostEqual(m.values["rt"], 0.5)
        self.assertEqual(drain(fd), [(a.EV_ABS, 0x05, 511)])
        m.update({"D": 3.5}, 0.01)
        self.assertIn((a.EV_ABS, 0x00, 32767), drain(fd))
        os.close(fd)

    def test_release_lets_go_of_everything(self):
        m = self.mapper("W=rt", "Q=a@1.0")
        fd = hold(m.pad.path)
        m.update({"W": 3.5, "Q": 3.0}, 0.01)
        drain(fd)
        m.release()
        self.assertEqual(set(drain(fd)), {(a.EV_ABS, 0x05, 0), (a.EV_KEY, 0x130, 0)})
        os.close(fd)

    def test_curve_gives_finer_control(self):
        m = self.mapper("W=rt", curve=2.0)
        m.update({"W": 1.9}, 0.01)
        self.assertAlmostEqual(m.values["rt"], 0.25)

    def test_mouse_speed_follows_depth_and_keeps_fractions(self):
        m = self.mapper("J=mouse-x+", "U=wheel+", "H=mouse-left")
        fd = hold(m.mouse.path)
        for _ in range(10):
            m.update({"J": 3.5}, 0.01)  # 1200 px/s for 0.1 s
        self.assertEqual(sum(v for k, c, v in drain(fd) if (k, c) == (a.EV_REL, 0x00)), 120)
        m.update({"H": 3.0}, 0.01)
        self.assertIn((a.EV_KEY, 0x110, 1), drain(fd))
        m.update({}, 0.01)
        self.assertIn((a.EV_KEY, 0x110, 0), drain(fd))
        os.close(fd)

    def test_two_stage_keystroke(self):
        m = self.mapper("J=key:F23@0.5", "J=key:F24@3.0", pause_key=None)
        m.virtual_keys = a.VirtualKeys(m.key_outputs)
        fd = hold(m.virtual_keys.path)
        m.update({"J": 1.0}, 0.01)
        self.assertEqual(drain(fd), [(a.EV_KEY, 193, 1)])
        m.update({"J": 3.2}, 0.01)
        self.assertEqual(drain(fd), [(a.EV_KEY, 194, 1)])
        m.update({"J": 0.0}, 0.01)
        self.assertEqual(set(drain(fd)), {(a.EV_KEY, 193, 0), (a.EV_KEY, 194, 0)})
        os.close(fd)

    def test_swallow_blocks_mapped_keys_forwards_the_rest_and_pauses(self):
        f21, f22, f23 = 191, 192, 193  # unused keys, in case anything got past the holds
        physical = a._Uinput("apex-analog test keyboard", (a.BUS_VIRTUAL, 0x1209, 0x00FF, 1), keys=[f21, f22, f23])
        self.addCleanup(physical.close)
        node = physical.path
        for _ in range(100):
            if node and os.access(node, os.R_OK):
                break
            time.sleep(0.02)
        output = a.VirtualKeys([f21, f22])
        self.addCleanup(output.close)
        out = hold(output.path)
        swallow = a.Swallow({f21}, output, pause_code=f23, nodes=[node])
        self.assertEqual(swallow.capabilities(), {f21, f22, f23})
        self.assertTrue(swallow.grab())
        self.addCleanup(swallow.stop)

        def tap(code):
            physical.emit(a.EV_KEY, code, 1)
            physical.sync()
            physical.emit(a.EV_KEY, code, 0)
            physical.sync()

        tap(f21)
        self.assertEqual(drain(out, 0.1), [])
        tap(f22)
        self.assertEqual(drain(out), [(a.EV_KEY, f22, 1), (a.EV_KEY, f22, 0)])
        tap(f23)  # pause: the mapped key types, and the pause key itself never does
        time.sleep(0.05)
        self.assertTrue(swallow.paused)
        tap(f21)
        self.assertEqual(drain(out), [(a.EV_KEY, f21, 1), (a.EV_KEY, f21, 0)])
        os.close(out)


class FakeMidi:
    def __init__(self):
        self.sent = []

    def note_on(self, channel, note, velocity):
        self.sent.append(("on", note, velocity))

    def note_off(self, channel, note, velocity=64):
        self.sent.append(("off", note))

    def key_pressure(self, channel, note, pressure):
        self.sent.append(("pressure", note, pressure))

    def controller(self, channel, number, value):
        self.sent.append(("cc", number, value))

    def channel_pressure(self, channel, pressure):
        self.sent.append(("channel", pressure))

    def pitch_bend(self, channel, value):
        self.sent.append(("bend", value))

    def close(self):
        pass


class Midi(unittest.TestCase):
    def instrument(self, **options):
        return a.MidiInstrument(output=FakeMidi(), swallow=False, pause_key=None, **options)

    def velocity_of(self, step):
        m = self.instrument()
        for mm in (0.0, 0.5, 1.0, 2.0):
            m.update({"Z": mm}, step)
        return next(e[2] for e in m.midi.sent if e[0] == "on")

    def test_layouts(self):
        piano = a.midi_layout("piano")
        self.assertEqual((piano["Z"], piano["S"], piano["Q"], piano[","]), (48, 49, 60, 60))
        self.assertEqual(a.midi_layout("piano", 1)["Z"], 60)
        grid = a.midi_layout("grid")
        self.assertEqual(grid["A"] - grid["Z"], 5)
        self.assertEqual(grid["X"] - grid["Z"], 1)
        with self.assertRaisesRegex(ValueError, "layout"):
            a.midi_layout("nope")

    def test_faster_press_is_louder(self):
        self.assertGreater(self.velocity_of(0.003), self.velocity_of(0.05))
        self.assertEqual(a.midi_velocity(1), 1)
        self.assertEqual(a.midi_velocity(10000), 127)

    def test_note_has_one_start_one_end_and_pressure_between(self):
        m = self.instrument()
        for mm in (2.0, 2.0, 3.0, 4.0, 1.0):
            m.update({"Z": mm}, 0.01)
        kinds = [e[0] for e in m.midi.sent]
        self.assertEqual(kinds.count("on"), 1)
        self.assertEqual(kinds.count("off"), 1)
        self.assertEqual(max(e[2] for e in m.midi.sent if e[0] == "pressure"), 127)
        self.assertLess(kinds.index("on"), kinds.index("pressure"))
        self.assertLess(kinds.index("pressure"), kinds.index("off"))

    def test_two_keys_on_one_note(self):
        m = self.instrument()
        m.update({",": 2.0}, 0.01)
        m.update({",": 2.0, "Q": 2.0}, 0.01)
        m.update({",": 0.0, "Q": 2.0}, 0.01)
        self.assertEqual([e[0] for e in m.midi.sent if e[0] in ("on", "off")], ["on"])
        m.update({}, 0.01)
        self.assertEqual([e[0] for e in m.midi.sent if e[0] in ("on", "off")], ["on", "off"])

    def test_sustain_mod_and_bend(self):
        m = self.instrument(ccs={"F": 74})
        m.update({"SPACE": 4.0, "LSHIFT": 2.0, "F": 4.0, "TAB": 4.0}, 0.01)
        self.assertIn(("cc", 64, 127), m.midi.sent)
        self.assertIn(("cc", 1, 63), m.midi.sent)
        self.assertIn(("cc", 74, 127), m.midi.sent)
        self.assertIn(("bend", 8191), m.midi.sent)
        m.update({"CAPS": 4.0}, 0.01)
        self.assertIn(("bend", -8191), m.midi.sent)
        m.update({}, 0.01)
        self.assertEqual(m.midi.sent[-1], ("bend", 0))

    def test_release_stops_every_note(self):
        m = self.instrument()
        m.update({"Z": 2.0, "X": 2.0}, 0.01)
        m.release()
        self.assertEqual(sum(e[0] == "off" for e in m.midi.sent), 2)
        self.assertEqual(m.midi.sent[-1], ("cc", 123, 0))
        self.assertEqual(m.sounding, {})

    @unittest.skipUnless(shutil.which("aseqdump") and os.access("/dev/snd/seq", os.W_OK), "no ALSA sequencer access")
    def test_real_alsa_port_reaches_a_real_client(self):
        midi = a.AlsaMidi("apex-analog test")
        self.addCleanup(midi.close)
        dump = subprocess.Popen(["aseqdump", "-p", midi.address], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        time.sleep(0.4)
        midi.note_on(0, 60, 100)
        midi.key_pressure(0, 60, 77)
        midi.controller(0, 64, 127)
        midi.pitch_bend(0, 512)
        midi.channel_pressure(0, 33)
        midi.note_off(0, 60)
        time.sleep(0.3)
        dump.terminate()
        out = dump.communicate(timeout=3)[0]
        for wanted in ("Note on", "note 60, velocity 100", "Polyphonic aftertouch", "value 77", "Control change", "controller 64, value 127", "Pitch bend", "512", "Channel aftertouch", "33", "Note off"):
            self.assertIn(wanted, out)


class HowYouType(unittest.TestCase):
    def press(self, key, down, held=0.09, peak=3.8, speed=120.0):
        return a.Press(key, down, down + held, peak, speed)

    def attempt(self, rng, peak=3.8, speed=120.0, gap=0.14):
        t, out = 0.0, []
        for c in a.TYPEPRINT_PHRASE:
            out.append(self.press(c, t, rng.gauss(0.09, 0.006), rng.gauss(peak, 0.08), rng.gauss(speed, 8)))
            t += rng.gauss(gap, 0.01)
        return a.typing_features(out)

    def test_press_log_measures_a_press(self):
        log = a._PressLog()
        out = []
        for now, mm in enumerate((0.0, 1.0, 3.0, 3.8, 2.0, 0.1)):
            out += log.update({"J": mm}, now * 0.01)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0].key, "J")
        self.assertEqual(out[0].peak, 3.8)
        self.assertAlmostEqual(out[0].speed, 200.0)
        self.assertAlmostEqual(out[0].up - out[0].down, 0.04)

    def test_typeprint_passes_you_and_rejects_someone_else(self):
        rng = random.Random(7)
        typeprint = a.build_typeprint([self.attempt(rng) for _ in range(a.TYPEPRINT_SAMPLES)])
        self.assertLessEqual(a.typeprint_score(typeprint, self.attempt(rng)), typeprint["limit"])
        self.assertGreater(a.typeprint_score(typeprint, self.attempt(rng, peak=2.4, speed=50, gap=0.28)), typeprint["limit"])

    def test_phrase_is_fixed_so_a_real_password_cant_end_up_on_disk(self):
        self.assertNotIn("--phrase", " ".join(a.build_parser().format_help().split()))
        self.assertEqual(a.TYPEPRINT_PHRASE, "pressure never lies")

    def test_profile_keeps_totals_and_never_the_order(self):
        profile = a.TypingProfile()
        for i, key in enumerate("hello"):
            profile.add(self.press(key.upper(), i * 0.1, peak=3.9), actuates_at=1.89)
        profile.add(self.press("H", 1.0, peak=1.0), actuates_at=1.89)
        self.assertEqual(sorted(profile.keys), ["E", "H", "L", "O"])
        self.assertEqual(profile.keys["L"]["count"], 2)
        self.assertEqual(profile.keys["H"]["near_misses"], 1)
        self.assertEqual(set(profile.keys["H"]), {"count", "bins", "speed", "held", "near_misses"})
        report = "\n".join(profile.report(1.89))
        self.assertIn("6 presses on 4 keys", report)
        self.assertIn("1 presses (16.7%) stopped short", report)

    def test_health_flags_only_what_is_off(self):
        self.assertEqual(a.key_health(15, 0.8, 2488, 2488), [])
        self.assertIn("high", a.key_health(200, 0.8, 2488, 2488)[0])
        self.assertIn("low", a.key_health(-50, 0.8, 2488, 2488)[0])
        self.assertIn("noisy", a.key_health(15, 9.0, 2488, 2488)[0])
        self.assertIn("range", a.key_health(15, 0.8, 1800, 2488)[0])


class Overlay(unittest.TestCase):
    def setUp(self):
        self.sink = a.ServeSink(0)
        self.port = self.sink.server.getsockname()[1]
        self.addCleanup(self.sink.server.close)

    def get(self, path, *headers):
        with socket.create_connection(("127.0.0.1", self.port), timeout=2) as c:
            c.sendall(("GET " + path + " HTTP/1.1\r\nHost: x\r\n" + "".join(h + "\r\n" for h in headers) + "\r\n").encode())
            out = b""
            while True:
                chunk = c.recv(65536)
                if not chunk:
                    return out.decode()
                out += chunk

    def test_page_latest_reading_and_outside_pages_refused(self):
        self.assertIn("<!DOCTYPE html>", self.get("/overlay"))
        self.assertIn("new WebSocket", self.get("/overlay"))
        self.sink.send({"W": 1.5})
        self.assertTrue(self.get("/").endswith('{"W": 1.5}'))
        self.assertIn("403", self.get("/overlay", "Origin: https://evil.example").split("\r\n")[0])


class Rules2(unittest.TestCase):
    def test_new_outputs(self):
        self.assertEqual(a.Mapping("I=p2:rt").player, 2)
        self.assertEqual(a.Mapping("LSHIFT=clutch").kind, "pedal")
        self.assertEqual(a.Mapping("X=tap:RIGHT").target, 106)
        self.assertEqual(a.Mapping("V=volume+").kind, "run")
        self.assertEqual(a.Mapping("V=run:wpctl x @SINK@ 2%+").target, "wpctl x @SINK@ 2%+")
        with self.assertRaisesRegex(ValueError, "p2"):
            a.Mapping("W=p2:key:F13")

    def test_every_preset_parses(self):
        for preset in a.PRESETS:
            self.assertTrue([a.Mapping(r) for r in a.expand_maps([preset])])


@unittest.skipUnless(HAVE_UINPUT, "no /dev/uinput access")
class Modes(unittest.TestCase):
    def mapper(self, *rules, **options):
        mapper = a.Mapper(list(rules), swallow=False, pause_key=None, **options)
        self.addCleanup(mapper.close)
        return mapper

    def test_two_players_are_separate_controllers(self):
        m = self.mapper("drive2")
        one, two = hold(m.pads[1].path), hold(m.pads[2].path)
        m.update({"W": 3.5, "J": 3.5}, 0.01)
        self.assertEqual(drain(one), [(a.EV_ABS, 0x05, 1023)])
        self.assertEqual(drain(two), [(a.EV_ABS, 0x00, -32767)])
        self.assertEqual(m.values["p2:lx"], -1.0)
        os.close(one)
        os.close(two)

    def test_any_key_can_be_its_own_axis_eight_to_a_device(self):
        m = self.mapper("A=axis:1", "S=axis:2", "D=axis:9")
        self.assertEqual(len(m.axes.paths), 2)
        first, second = hold(m.axes.paths[0]), hold(m.axes.paths[1])
        m.update({"A": 3.5, "D": 1.9}, 0.01)
        self.assertEqual(drain(first), [(a.EV_ABS, 0x00, 4095)])
        self.assertEqual(drain(second), [(a.EV_ABS, 0x00, 2047)])
        self.assertEqual(m.values["axis:1"], 1.0)
        os.close(first)
        os.close(second)

    def test_two_keys_can_share_one_axis(self):
        m = self.mapper("LSHIFT=axis:1+", "LCTRL=axis:1-")
        fd = hold(m.axes.paths[0])
        m.update({}, 0.01)  # rests in the middle, which was sent when it was made
        self.assertEqual(m.values["axis:1"], 0.5)
        m.update({"LSHIFT": 3.5}, 0.01)
        m.update({"LCTRL": 3.5}, 0.01)
        self.assertEqual(drain(fd), [(a.EV_ABS, 0x00, 4095), (a.EV_ABS, 0x00, 0)])
        os.close(fd)

    def test_pedals_are_their_own_device(self):
        m = self.mapper("manual")
        fd = hold(m.pedals.path)
        m.update({"LSHIFT": 3.5, "SPACE": 1.9}, 0.01)
        self.assertEqual(set(drain(fd)), {(a.EV_ABS, 0x00, 4095), (a.EV_ABS, 0x01, 2047)})
        m.release()
        self.assertEqual(set(drain(fd)), {(a.EV_ABS, 0x00, 0), (a.EV_ABS, 0x01, 0)})
        os.close(fd)

    def test_tap_rate_follows_depth(self):
        m = self.mapper("J=tap:F24")
        m.virtual_keys = a.VirtualKeys(m.key_outputs)
        fd = hold(m.virtual_keys.path)
        m.update({"J": 3.5}, 0.1)  # one straight away, then 20 a second for 0.1 s
        self.assertEqual(drain(fd), [(a.EV_KEY, 194, 1), (a.EV_KEY, 194, 0)] * 3)
        m.update({}, 0.1)
        self.assertEqual(drain(fd, 0.05), [])
        os.close(fd)

    def test_run_starts_a_command_and_never_overlaps(self):
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "ran")
            m = self.mapper(f"J=run:sleep 0.2; echo x >> {path}")
            for _ in range(5):
                m.update({"J": 3.5}, 0.5)
            m.maps[0].process.wait(timeout=3)
            with open(path) as f:
                self.assertEqual(f.read(), "x\n")

    def typer(self, **options):
        typer = a.PressureTyper(pause_key=None, **options)
        typer.virtual_keys = a.VirtualKeys(typer.key_outputs)
        self.addCleanup(typer.close)
        return typer, hold(typer.virtual_keys.path)

    def test_light_press_is_lowercase_after_it_settles(self):
        t, fd = self.typer()
        t.update({"J": 2.0}, 0.01)
        self.assertEqual(drain(fd, 0.05), [])
        t.update({"J": 1.6}, 0.01)
        self.assertEqual(drain(fd), [(a.EV_KEY, 36, 1)])
        t.update({}, 0.01)
        self.assertEqual(drain(fd), [(a.EV_KEY, 36, 0)])
        os.close(fd)

    def test_deep_press_is_a_capital(self):
        t, fd = self.typer()
        t.update({"J": 2.0}, 0.01)
        t.update({"J": 3.5}, 0.01)
        self.assertEqual(drain(fd), [(a.EV_KEY, 42, 1), (a.EV_KEY, 36, 1), (a.EV_KEY, 42, 0)])
        t.update({}, 0.01)
        self.assertEqual(drain(fd), [(a.EV_KEY, 36, 0)])
        os.close(fd)

    def test_keys_go_out_in_the_order_pressed(self):
        t, fd = self.typer()
        t.update({"K": 2.0}, 0.01)
        t.update({"K": 2.0, "A": 2.0}, 0.01)  # A sorts first but was pressed second
        self.assertEqual(drain(fd), [(a.EV_KEY, 37, 1)])
        t.update({"K": 0.0, "A": 3.5}, 0.01)
        self.assertEqual(drain(fd), [(a.EV_KEY, 42, 1), (a.EV_KEY, 30, 1), (a.EV_KEY, 42, 0), (a.EV_KEY, 37, 0)])
        os.close(fd)

    def test_a_quick_tap_still_types(self):
        t, fd = self.typer()
        t.update({"J": 2.0}, 0.01)
        t.update({}, 0.01)
        self.assertEqual(drain(fd), [(a.EV_KEY, 36, 1), (a.EV_KEY, 36, 0)])
        os.close(fd)

    def test_held_key_settles_so_it_can_repeat(self):
        t, fd = self.typer()
        t.update({"J": 2.0}, 0.01)
        t.update({"J": 2.0}, 0.2)
        self.assertEqual(drain(fd), [(a.EV_KEY, 36, 1)])
        os.close(fd)

    def test_shortcuts_stay_instant(self):
        t, fd = self.typer()
        t.update({"LCTRL": 3.0, "C": 2.0}, 0.01)
        self.assertEqual(drain(fd), [(a.EV_KEY, 46, 1)])
        os.close(fd)

    def test_deep_combo_replaces_the_capital(self):
        t, fd = self.typer(combos={"K": "F24"})
        t.update({"K": 3.5}, 0.01)
        t.update({"K": 3.5}, 0.01)
        self.assertEqual(drain(fd), [(a.EV_KEY, 194, 1), (a.EV_KEY, 194, 0)])
        t.update({}, 0.01)
        self.assertEqual(drain(fd, 0.05), [])
        os.close(fd)


class FakeLightKeyboard:
    def __init__(self):
        self.calls = []

    def set_all_colors(self, r, g, b):
        self.calls.append(("all", r, g, b))

    def set_colors(self, colors):
        self.calls.append(("keys", dict(colors)))

    def clear_colors(self):
        self.calls.append(("clear",))


class LightingBytes(unittest.TestCase):
    def setUp(self):
        self.read, write = os.pipe()
        self.kb = a.Keyboard.__new__(a.Keyboard)
        self.kb.fd = write
        self.addCleanup(os.close, self.read)
        self.addCleanup(os.close, write)

    def packets(self, count):
        return [os.read(self.read, 65) for _ in range(count)]

    def test_one_key(self):
        self.kb.set_colors({"W": (1, 2, 3)})
        packet = self.packets(1)[0]
        self.assertEqual(len(packet), 65)
        self.assertEqual(packet[:7], bytes([0, 0x3A, 1, 0x1A, 1, 2, 3]))
        self.assertEqual(set(packet[7:]), {0})

    def test_many_keys_split_into_reports_that_fit(self):
        self.kb.set_colors({k: (9, 9, 9) for k in "QWERTYUIOPASDFGH"})
        first, second = self.packets(2)
        self.assertEqual(first[:2], bytes([0, 0x3A]))
        self.assertEqual(first[2], 15)
        self.assertEqual(second[2], 1)
        self.assertEqual(second[3], a.BY_NAME["H"][1])

    def test_whole_board_sets_every_led_key_individually(self):
        self.kb.set_all_colors(4, 5, 6)
        reports = (len(a.LED_HIDS) + 14) // 15
        seen = []
        for packet in self.packets(reports):
            self.assertEqual(packet[:2], bytes([0, 0x3A]))
            for i in range(packet[2]):
                self.assertEqual(packet[4 + i * 4:7 + i * 4], bytes([4, 5, 6]))
                seen.append(packet[3 + i * 4])
        self.assertEqual(seen, a.LED_HIDS)
        for hid, _, _ in a.CHANNELS.values():
            self.assertIn(hid, a.LED_HIDS)
        for number_row_and_nav in (0x29, 0x3A, 0x45, 0x4A, 0x52):  # ESC, F1, F12, HOME, UP have LEDs but no sensor
            self.assertIn(number_row_and_nav, a.LED_HIDS)

    def test_restore(self):
        self.kb.clear_colors()
        self.assertEqual(self.packets(1)[0][:2], bytes([0, 0x3B]))

    def test_every_write_leaves_a_gap_even_the_last(self):
        naps = []
        original = a.time.sleep
        a.time.sleep = naps.append
        self.addCleanup(setattr, a.time, "sleep", original)
        self.kb.set_all_colors(0, 0, 0)
        self.assertEqual(naps, [a.WRITE_GAP] * ((len(a.LED_HIDS) + 14) // 15))
        naps.clear()
        self.kb.clear_colors()
        self.assertEqual(naps, [a.WRITE_GAP])
        naps.clear()
        self.kb.set_actuation("W", 1.0)
        self.assertEqual(naps, [a.WRITE_GAP])
        self.assertGreaterEqual(a.WRITE_GAP, 0.002)

    def test_only_lighting_commands_get_through(self):
        for command in (0x31, 0x30, 0x01, 0x5A, 0x7F, 0x74, 0x15, 0x16, 0x51, 0x22, 0x21):
            with self.assertRaisesRegex(ValueError, "refused"):
                self.kb._send_light([command])
        self.assertEqual(set(a.LIGHT_COMMANDS), {0x3A, 0x3B})

    def test_reads_cannot_send_lighting_commands(self):
        for command in a.LIGHT_COMMANDS:
            with self.assertRaisesRegex(ValueError, "refused"):
                self.kb._ask([command])


class LightsLogic(unittest.TestCase):
    def test_glow_follows_depth_fades_and_stops_sending_when_still(self):
        kb, lights = FakeLightKeyboard(), a.Lights((200, 0, 0), (0, 0, 0), fade=0.2, hz=30)
        lights.start(kb)
        self.assertEqual(kb.calls.pop(), ("all", 0, 0, 0))
        lights.update(kb, {"W": 4.0, "A": 0.0}, 1.0)
        self.assertEqual(kb.calls.pop(), ("keys", {"W": (200, 0, 0)}))
        lights.update(kb, {"W": 4.0, "A": 0.0}, 1.1)
        self.assertEqual(kb.calls, [])
        lights.update(kb, {"W": 0.0, "A": 0.0}, 1.2)
        self.assertEqual(kb.calls.pop(), ("keys", {"W": (100, 0, 0)}))
        lights.update(kb, {"W": 0.0, "A": 0.0}, 1.4)
        self.assertEqual(kb.calls.pop(), ("keys", {"W": (0, 0, 0)}))

    def test_board_goes_dark_first_by_default(self):
        kb = FakeLightKeyboard()
        a.Lights().start(kb)
        self.assertEqual(kb.calls, [("all", 0, 0, 0)])
        args = a.build_parser().parse_args(["lights"])
        self.assertEqual(a._lights_from(args).base, (0, 0, 0))

    def test_keep_mode_leaves_normal_lighting_until_a_press(self):
        kb, lights = FakeLightKeyboard(), a.GlowOverNormal(fade=0, hz=30)
        lights.start(kb)
        self.assertEqual(kb.calls, [("clear",)])
        kb.calls.clear()
        lights.update(kb, {"W": 0.0, "A": 0.0}, 1.0)
        self.assertEqual(kb.calls, [])

    def test_keep_mode_gets_brighter_white_with_depth(self):
        kb, lights = FakeLightKeyboard(), a.GlowOverNormal(fade=0, hz=30)
        lights.update(kb, {"W": 1.0}, 1.0)
        shallow = kb.calls.pop()[1]["W"]
        lights.update(kb, {"W": 4.0}, 1.1)
        deep = kb.calls.pop()[1]["W"]
        self.assertEqual(deep, (255, 255, 255))
        self.assertEqual(shallow[0], shallow[1])
        self.assertEqual(shallow[1], shallow[2])
        self.assertLess(0, shallow[0])
        self.assertLess(shallow[0], deep[0])

    def test_keep_mode_letting_go_restores_normal_and_keeps_held_keys(self):
        kb, lights = FakeLightKeyboard(), a.GlowOverNormal(fade=0, hz=30)
        lights.update(kb, {"W": 4.0, "A": 2.0}, 1.0)
        kb.calls.clear()
        lights.update(kb, {"W": 0.0, "A": 2.0}, 1.1)
        self.assertEqual(kb.calls[0], ("clear",))
        self.assertEqual(list(kb.calls[1][1]), ["A"])
        kb.calls.clear()
        lights.update(kb, {"W": 0.0, "A": 2.0}, 1.2)
        self.assertEqual(kb.calls, [])
        lights.update(kb, {"W": 0.0, "A": 0.0}, 1.3)
        self.assertEqual(kb.calls, [("clear",)])

    def test_keep_flag_picks_white_and_works_on_other_modes(self):
        chosen = a._lights_from(a.build_parser().parse_args(["lights", "--keep"]))
        self.assertIsInstance(chosen, a.GlowOverNormal)
        self.assertEqual(chosen.color, (255, 255, 255))
        self.assertEqual(a._lights_from(a.build_parser().parse_args(["gamepad", "--lights", "--keep", "--color", "00C8FF"])).color, (0, 200, 255))
        dark = a._lights_from(a.build_parser().parse_args(["lights"]))
        self.assertNotIsInstance(dark, a.GlowOverNormal)
        self.assertEqual((dark.color, dark.fade), ((255, 90, 0), 0.25))

    def test_rate_is_capped(self):
        kb, lights = FakeLightKeyboard(), a.Lights(hz=30)
        lights.update(kb, {"W": 4.0}, 1.0)
        kb.calls.clear()
        lights.update(kb, {"W": 1.0}, 1.001)
        self.assertEqual(kb.calls, [])

    def test_quitting_restores_even_if_the_keyboard_is_gone(self):
        class Gone(FakeLightKeyboard):
            def clear_colors(self):
                raise a.Disconnected("gone")
        a.Lights().stop(Gone())
        kb = FakeLightKeyboard()
        a.Lights().stop(kb)
        self.assertEqual(kb.calls, [("clear",)])

    def test_bad_colour_explains_itself(self):
        self.assertEqual(a._rgb("FF5A00"), (255, 90, 0))
        self.assertEqual(a._rgb("#0a0a14"), (10, 10, 20))
        with self.assertRaisesRegex(ValueError, "colour"):
            a._rgb("red")


class Actuation(unittest.TestCase):
    def setUp(self):
        self.read, write = os.pipe()
        self.kb = a.Keyboard.__new__(a.Keyboard)
        self.kb.fd = write
        self.addCleanup(os.close, self.read)
        self.addCleanup(os.close, write)

    def test_levels_match_what_the_keyboard_uses(self):
        self.assertEqual(a.actuation_level(1.8), (1.8, 50, 46))  # the keyboard's own default here
        self.assertEqual(a.actuation_level(1.0), (1.0, 23, 21))  # the value tested by hand on W
        self.assertEqual(a.actuation_level(0.1), (0.1, 5, 5))
        self.assertEqual(a.actuation_level(4.0), (4.0, 215, 210))
        self.assertEqual(a.actuation_level(1.9)[0], 1.8)

    def test_every_level_has_sane_points(self):
        for tenths in a.LEVEL_TENTHS:
            _, press, release = a.actuation_level(tenths / 10)
            self.assertGreaterEqual(press, release)

    def test_out_of_range_is_refused(self):
        for bad in (0.0, 4.5, -1):
            with self.assertRaisesRegex(ValueError, "between"):
                a.actuation_level(bad)

    def test_exact_bytes_for_one_key(self):
        self.assertEqual(self.kb.set_actuation("W", 1.0), 1.0)
        packet = os.read(self.read, 65)
        self.assertEqual(packet[:6], bytes([0, 0x31, 1, 0x1A, 23, 21]))
        self.assertEqual(set(packet[6:]), {0})

    def test_many_keys_split_and_lgui_is_skipped(self):
        self.kb.set_actuation(list("QWERTYUIOPASDFGHJKLZXC") + ["LGUI"], 2.0)
        first, second = os.read(self.read, 65), os.read(self.read, 65)
        self.assertEqual((first[2], second[2]), (20, 2))
        self.assertNotIn(a.BY_NAME["LGUI"][1], second[3:9:3])

    def test_settings_command_list_stays_short(self):
        self.assertEqual(set(a.SETTING_COMMANDS), {0x31, 0x32, 0x33})
        self.assertEqual(a.SAVE_COMMAND, 0x09)


class FakeKb(FakeLightKeyboard):
    keys = ["W", "A", "S", "D"]

    def set_actuation_points(self, names, press, release):
        self.calls.append(("points", sorted(names), press, release))

    def set_mode(self, names, mode):
        self.calls.append(("mode", sorted(names), mode))


class EffectEngine(unittest.TestCase):
    class Fixed(a.Effect):
        def __init__(self, frames, **options):
            super().__init__(**options)
            self.frames = frames

        def frame(self, depths, now, elapsed):
            return self.frames.pop(0)

    def test_dark_mode_sends_only_changes_and_returns_keys_to_base(self):
        kb = FakeKb()
        effect = self.Fixed([{"W": (1, 1, 1)}, {"W": (1, 1, 1)}, {}], base=(2, 2, 2))
        effect.start(kb)
        self.assertEqual(kb.calls.pop(), ("all", 2, 2, 2))
        effect.update(kb, {}, 1.0)
        self.assertEqual(kb.calls.pop(), ("keys", {"W": (1, 1, 1)}))
        effect.update(kb, {}, 1.1)
        self.assertEqual(kb.calls, [])
        effect.update(kb, {}, 1.2)
        self.assertEqual(kb.calls.pop(), ("keys", {"W": (2, 2, 2)}))

    def test_keep_mode_releases_when_nothing_is_left_and_limits_how_often(self):
        kb = FakeKb()
        effect = self.Fixed([{"W": (1, 1, 1), "A": (1, 1, 1)}, {"A": (1, 1, 1)}, {"A": (1, 1, 1)}, {}], keep=True)
        effect.last_clear = 1.0
        effect.update(kb, {}, 1.0)
        kb.calls.clear()
        effect.update(kb, {}, 1.1)  # W ended, but a release was too recent
        self.assertEqual(kb.calls, [])
        effect.update(kb, {}, 1.3)  # now it's allowed: release all, re-send what's still on
        self.assertEqual(kb.calls[0], ("clear",))
        self.assertEqual(kb.calls[1], ("keys", {"A": (1, 1, 1)}))
        kb.calls.clear()
        effect.update(kb, {}, 1.35)
        self.assertEqual(kb.calls, [("clear",)])

    def test_every_key_used_by_effects_has_an_led(self):
        for key in a.POSITIONS:
            self.assertIn(a.led_hid(key), a.LED_HIDS, key)
        for row in a.STAT_ROWS + [a.RevLights.ROW]:
            for key in row:
                self.assertIn(key, a.POSITIONS)
        for name in a.BY_NAME:
            if not name.startswith("INT") and name not in ("#", "ISO\\"):
                self.assertIn(name, a.POSITIONS)

    def test_led_names(self):
        self.assertEqual(a.led_hid("esc"), 0x29)
        self.assertEqual(a.led_hid("F12"), 0x45)
        self.assertEqual(a.led_hid("w"), 0x1A)
        with self.assertRaisesRegex(ValueError, "LED"):
            a.led_hid("nope")

    def test_ripple_travels_outward_then_ends(self):
        ripple = a.Ripple((200, 0, 0))
        ripple.frame({"W": 0.0}, 0.0, 0.04)
        first = ripple.frame({"W": 2.0}, 0.04, 0.04)
        self.assertIn("W", first)
        self.assertNotIn("L", first)
        later = ripple.frame({"W": 0.0}, 0.04 + 7 / a.Ripple.SPEED, 0.04)
        self.assertIn("I", later)
        self.assertNotIn("W", later)
        self.assertEqual(ripple.frame({"W": 0.0}, 2.0, 0.04), {})

    def test_audio_bands_find_the_right_pitch_and_ignore_silence(self):
        rate, count = 11025, 256
        tone = [0.5 * math.sin(2 * math.pi * 600 * i / rate) for i in range(count)]
        bands = a.audio_bands(tone, rate)
        centre = [60.0 * (4500.0 / 60.0) ** (i / 13) for i in range(14)]
        loudest = max(range(14), key=lambda i: bands[i])
        self.assertLess(abs(math.log(centre[loudest] / 600)), 0.6)
        self.assertEqual(a.audio_bands([0.0] * count, rate), [0.0] * 14)

    def test_music_bars_fill_from_the_bottom_row(self):
        bars = a.MusicBars().bars([0.25] * 14)
        self.assertIn("SPACE", bars)
        self.assertNotIn("F1", bars)
        self.assertEqual(a.MusicBars().bars([0.0] * 14), {})
        self.assertIn("F1", a.MusicBars().bars([1.0] * 14))

    def test_stats_meter_lights_the_right_share_of_a_row(self):
        class Stats:
            def cpu(self):
                return 0.5

            def ram(self):
                return 1.0
        meter = a.StatsMeter(("cpu", "ram"), Stats())
        wanted = meter.frame({}, 1.0, 1.0)
        self.assertEqual(sum(wanted[f"F{i}"] != (0, 0, 0) for i in range(1, 13)), 6)
        self.assertTrue(all(wanted[k] != (0, 0, 0) for k in "1234567890-="))
        self.assertNotIn("Q", wanted)
        with self.assertRaisesRegex(ValueError, "--stats"):
            a.StatsMeter(("nope",))

    def test_real_stats_read_in_range(self):
        stats = a.SystemStats()
        stats.cpu()
        for name in ("cpu", "ram", "temp", "battery"):
            self.assertTrue(0.0 <= getattr(stats, name)() <= 1.0, name)

    def packet(self, gear=2, rpm=3000.0, show=0):
        return a.OUTGAUGE.pack(0, b"beam", 0, gear, 0, 20.0, rpm, 0.0, 90.0, 0.5, 0.0, 90.0, 1, show, 0.5, 0.0, 0.0, b"", b"")

    def test_outgauge_matches_beamngs_struct_size_and_fields(self):
        self.assertEqual(a.OUTGAUGE.size, 92)
        self.assertEqual(a.parse_outgauge(self.packet(3, 4200.0, 1)), (3, 4200.0, True))
        self.assertEqual(a.parse_outgauge(self.packet() + bytes(4)), (2, 3000.0, False))
        self.assertIsNone(a.parse_outgauge(b"short"))

    def test_rev_lights_bar_gear_shift_flash_and_stale_data(self):
        rev = a.RevLights(redline=6000)
        rev.latest, rev.received = (3, 3000.0, False), 10.0
        wanted = rev.frame({}, 10.0, 0.03)
        self.assertEqual(sum(wanted[k] != (0, 0, 0) for k in rev.ROW), 6)
        self.assertEqual(wanted["F2"], (255, 255, 255))
        rev.latest = (3, 5900.0, False)
        flash = [rev.frame({}, 10.0 + i / 8, 0.03)["1"] for i in range(2)]
        self.assertNotEqual(flash[0], flash[1])
        rev.latest = (3, 1000.0, True)
        self.assertIn(rev.frame({}, 10.0, 0.03)["1"], ((255, 0, 0), (255, 255, 255)))
        rev.latest = (0, 1000.0, False)
        self.assertEqual(rev.frame({}, 10.0, 0.03)["ESC"], (255, 0, 0))
        self.assertEqual(rev.frame({}, 12.0, 0.03), {})

    def test_heatmap_uses_totals_and_explains_itself_with_none(self):
        heat = a.TypingHeatmap(totals={"E": 1000, "Q": 1})
        wanted = heat.frame({}, 1.0, 1.0)
        self.assertEqual(wanted["E"], a._heat_color(1.0))
        self.assertLess(wanted["Q"][0], wanted["E"][0] + 1)
        self.assertEqual(a.TypingHeatmap(totals={}).frame({}, 1.0, 1.0), {})
        original = a._state_path
        self.addCleanup(setattr, a, "_state_path", original)
        a._state_path = lambda name: "/nonexistent/x"
        with self.assertRaisesRegex(a.ApexError, "profile"):
            a.TypingHeatmap()

    def test_heatmap_changes_as_you_type(self):
        class Board:
            def __init__(self):
                self.sent = {}

            def set_colors(self, colors):
                self.sent.update(colors)

        def tap(heat, board, key, now):
            heat.update(board, {key: 2.0}, now)
            heat.update(board, {key: 0.0}, now + 0.01)

        board = Board()
        heat = a.TypingHeatmap(keep=True, totals={"E": 1000, "Q": 1})
        heat.update(board, {}, 1.0)
        before = board.sent["Q"]
        for i in range(50):
            tap(heat, board, "Q", 2.0 + i * 0.02)  # far quicker than it redraws
        self.assertEqual(heat.totals["Q"], 51)
        heat.update(board, {}, 10.0)
        self.assertNotEqual(board.sent["Q"], before)
        self.assertEqual(board.sent["Q"], a._heat_color(math.log(52) / math.log(1001)))

    def test_heatmap_lights_a_key_on_its_first_press(self):
        heat = a.TypingHeatmap(keep=True, totals={"E": 1000})
        self.assertNotIn("Z", heat.frame({}, 1.0, 1.0))
        heat.log.update({"Z": 2.0}, 1.0)
        heat.update(type("Board", (), {"set_colors": lambda self, colors: None})(), {"Z": 2.0}, 2.0)
        heat.update(type("Board", (), {"set_colors": lambda self, colors: None})(), {"Z": 0.0}, 2.1)
        self.assertEqual(heat.totals["Z"], 1)
        self.assertIn("Z", heat.frame({}, 3.0, 1.0))

    def test_heatmap_watches_every_key(self):
        self.assertIn("heatmap", a.USES_DEPTH)

    def test_effect_flags_choose_the_right_thing(self):
        parse = a.build_parser().parse_args
        for flags, kind in ((["--effect", "ripple"], a.Ripple), (["--effect", "music"], a.MusicBars), (["--effect", "stats"], a.StatsMeter),
                            (["--effect", "rev"], a.RevLights), (["--keep"], a.GlowOverNormal), ([], a.Lights)):
            self.assertIsInstance(a._lights_from(parse(["lights", *flags])), kind)
        self.assertTrue(a._lights_from(parse(["lights", "--effect", "ripple", "--keep"])).keep)
        self.assertEqual(a._lights_from(parse(["gamepad", "--lights", "--effect", "rev", "--port", "5000"])).port, 5000)


class Writes(unittest.TestCase):
    def setUp(self):
        self.read, write = os.pipe()
        self.kb = a.Keyboard.__new__(a.Keyboard)
        self.kb.fd = write
        self.kb.keys = list(a.BY_NAME)
        self.addCleanup(os.close, self.read)
        self.addCleanup(os.close, write)

    def packet(self):
        return os.read(self.read, 65)

    def test_rapid_tap_bytes(self):
        self.kb.set_mode("W", a.RAPID_TAP_MODE)
        self.assertEqual(self.packet()[:5], bytes([0, 0x32, 1, 0x1A, 2]))
        self.kb.set_mode("WA", 0)
        self.assertEqual(self.packet()[:7], bytes([0, 0x32, 2, 0x1A, 0, 0x04, 0]))

    def test_many_modes_split(self):
        self.kb.set_mode(list(a.BY_NAME)[:31], 0)
        self.assertEqual(self.packet()[2], 30)
        self.assertEqual(self.packet()[2], 1)

    def test_save_refuses_a_bad_slot_before_sending_anything(self):
        for bad in (0, 6, -1, 255, "1", None):
            with self.assertRaisesRegex(ValueError, "slot"):
                self.kb.save_to_keyboard(bad)
        self.assertFalse(select.select([self.read], [], [], 0)[0])

    def test_save_sends_exactly_the_one_command(self):
        self.kb._ask = lambda payload, valid=None: bytes([3])
        self.kb.save_to_keyboard(3)
        self.assertEqual(self.packet()[:3], bytes([0, 0x09, 3]))
        self.assertFalse(select.select([self.read], [], [], 0)[0])

    def test_save_waits_while_the_keyboard_is_busy(self):
        answers = [a.Disconnected("busy"), a.Disconnected("busy"), bytes([1])]

        def ask(payload, valid=None):
            answer = answers.pop(0)
            if isinstance(answer, Exception):
                raise answer
            return answer
        self.kb._ask = ask
        self.kb.save_to_keyboard(1)
        self.assertEqual(answers, [])

    def test_write_refuses_everything_else(self):
        for command in (0x01, 0x81, 0x58, 0x59, 0x5A, 0x74, 0x7F, 0x30, 0x33, 0x65):
            with self.assertRaisesRegex(ValueError, "refused"):
                self.kb._write([command])
        self.assertFalse(select.select([self.read], [], [], 0)[0])

    def test_apply_preset_groups_and_skips_keys_not_on_this_board(self):
        kb = FakeKb()
        a.apply_preset(kb, {"W": [23, 21, 2], "A": [23, 21, 0], "S": [50, 46, 0], "#": [1, 1, 0]})
        self.assertIn(("points", ["A", "W"], 23, 21), kb.calls)
        self.assertIn(("points", ["S"], 50, 46), kb.calls)
        self.assertIn(("mode", ["W"], 2), kb.calls)
        self.assertIn(("mode", ["A", "S"], 0), kb.calls)
        self.assertFalse([c for c in kb.calls if "#" in str(c)])


class Auto(unittest.TestCase):
    def setUp(self):
        self.titles = ["Firefox"]
        self.auto = a.AutoProfiles(
            {"rules": [{"window": "beamng", "preset": "gaming", "lights": "ripple"}], "default": {"lights": "glow-keep"}},
            {"gaming": {"W": [23, 21, 0]}}, title=lambda: self.titles[0])

    def test_matches_case_insensitively_and_falls_back(self):
        self.assertEqual(self.auto.choose("BeamNG.drive - 0.40").get("preset"), "gaming")
        self.assertIs(self.auto.choose("Firefox"), self.auto.default)

    def test_switching_applies_preset_and_swaps_effect_once(self):
        kb = FakeKb()
        self.auto.window = "Firefox"
        self.auto.update(kb, {}, 1.0)
        self.assertIsInstance(self.auto.effect, a.GlowOverNormal)
        kb.calls.clear()
        self.auto.window = "BeamNG.drive"
        self.auto.update(kb, {}, 2.0)
        self.assertIn(("points", ["W"], 23, 21), kb.calls)
        self.assertIsInstance(self.auto.effect, a.Ripple)
        kb.calls.clear()
        self.auto.update(kb, {}, 2.01)
        self.assertEqual([c for c in kb.calls if c[0] == "points"], [])
        self.auto.stop(kb)
        self.assertIn(("clear",), kb.calls)


class Files(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        saved = os.environ.get("XDG_CONFIG_HOME")
        os.environ["XDG_CONFIG_HOME"] = self.folder.name
        self.addCleanup(lambda: os.environ.pop("XDG_CONFIG_HOME") if saved is None else os.environ.__setitem__("XDG_CONFIG_HOME", saved))

    def run_cli(self, *argv):
        return a.build_parser().parse_args(list(argv))

    def test_autostart_writes_and_removes_its_own_file_only(self):
        args = self.run_cli("autostart", "on")
        a.cmd_autostart(args)
        path = a._autostart_path()
        self.assertTrue(path.startswith(self.folder.name))
        with open(path) as f:
            text = f.read()
        self.assertIn("apex-analog daemon", text.replace("apex_analog.py", "apex-analog"))
        self.assertIn("Type=Application", text)
        a.cmd_autostart(self.run_cli("autostart", "on", "lights", "--effect", "ripple"))
        with open(path) as f:
            self.assertIn("lights --effect ripple", f.read())
        other = os.path.join(os.path.dirname(path), "someone-else.desktop")
        with open(other, "w") as f:
            f.write("x")
        a.cmd_autostart(self.run_cli("autostart", "off"))
        self.assertFalse(os.path.exists(path))
        self.assertTrue(os.path.exists(other))

    def test_recorder_adds_up_saves_privately_and_keeps_no_order(self):
        os.makedirs(os.path.join(self.folder.name, "state"))
        path = os.path.join(self.folder.name, "state", "typing.json")
        recorder = a.TypingRecorder({"J": 1.89}, path, every=0.0)
        base = time.monotonic() + 1
        for now, mm in enumerate((0.0, 3.0, 4.0, 0.0)):
            recorder.update({"J": mm}, base + now * 0.01)
        self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)
        again = a.TypingRecorder({"J": 1.89}, path, every=0.0)
        self.assertEqual(again.profile.keys["J"]["count"], 1)
        for now, mm in enumerate((0.0, 3.0, 0.0)):
            again.update({"J": mm}, base + 1 + now * 0.01)
        self.assertEqual(a.TypingRecorder({}, path).profile.keys["J"]["count"], 2)
        with open(path) as f:
            import json
            self.assertEqual(set(json.load(f)), {"keys"})

    def test_macro_add_list_delete_and_private_file(self):
        a.cmd_macro(self.run_cli("macro", "add", "hi", "text:Hi!", "100", "LCTRL+C"))
        path = a._config_path("macros.json")
        self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)
        with open(path) as f:
            import json
            self.assertEqual(json.load(f)["hi"], [{"text": "Hi!"}, 100.0, "LCTRL+C"])
        a.cmd_macro(self.run_cli("macro", "delete", "hi"))
        with self.assertRaisesRegex(ValueError, "No macro"):
            a.cmd_macro(self.run_cli("macro", "delete", "hi"))
        with self.assertRaisesRegex(ValueError, "unknown key"):
            a.cmd_macro(self.run_cli("macro", "add", "bad", "NOPE+C"))

    def test_unknown_macro_in_a_rule_is_explained(self):
        with self.assertRaisesRegex(ValueError, "No macro called nope"):
            a.Mapper(["J=macro:nope"], swallow=False, pause_key=None)


class Macros(unittest.TestCase):
    def test_compile_combo_wait_and_text(self):
        ctrl, c = a.keycode("LCTRL"), a.keycode("C")
        events = a.compile_macro(["LCTRL+C", 100, {"text": "a"}])
        self.assertEqual([e[:2] for e in events[:4]], [(ctrl, 1), (c, 1), (c, 0), (ctrl, 0)])
        self.assertEqual(events[4][:2], (a.keycode("A"), 1))
        self.assertEqual(events[4][2], a.MACRO_GAP_MS + 100)

    def test_capitals_and_symbols_use_shift(self):
        shift, one = a.keycode("LSHIFT"), a.keycode("1")
        self.assertEqual([e[:2] for e in a.compile_macro([{"text": "!"}])], [(shift, 1), (one, 1), (one, 0), (shift, 0)])
        self.assertEqual(a.compile_macro([{"text": "B"}])[0][:2], (shift, 1))
        self.assertEqual(a.compile_macro([{"text": " "}])[0][:2], (a.keycode("SPACE"), 1))

    def test_recording_format(self):
        self.assertEqual(a.compile_macro([{"events": [[30, 1, 0], [30, 0, 50]]}]), [(30, 1, 0), (30, 0, 50)])

    def test_bad_step(self):
        with self.assertRaisesRegex(ValueError, "macro step"):
            a.compile_macro([[1, 2]])

    def test_play_never_leaves_a_key_held(self):
        class Output:
            def __init__(self):
                self.sent = []

            def send(self, code, value):
                self.sent.append((code, value))
        output = Output()
        a.play_macro(output, [(30, 1, 0), (31, 1, 0)])
        self.assertEqual(sorted(output.sent[2:]), [(30, 0), (31, 0)])

    @unittest.skipUnless(HAVE_UINPUT, "no /dev/uinput access")
    def test_deep_press_fires_once_per_press(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        saved = os.environ.get("XDG_CONFIG_HOME")
        os.environ["XDG_CONFIG_HOME"] = folder.name
        self.addCleanup(lambda: os.environ.pop("XDG_CONFIG_HOME") if saved is None else os.environ.__setitem__("XDG_CONFIG_HOME", saved))
        a._write_private(a._config_path("macros.json"), {"go": ["F24"]})
        m = a.Mapper(["J=macro:go@3.0"], swallow=False, pause_key=None)
        self.addCleanup(m.close)
        m.virtual_keys = a.VirtualKeys(m.key_outputs)
        fd = hold(m.virtual_keys.path)
        m.update({"J": 2.0}, 0.01)
        self.assertEqual(drain(fd, 0.1), [])
        m.update({"J": 3.5}, 0.01)
        m.update({"J": 3.6}, 0.01)
        self.assertEqual(drain(fd), [(a.EV_KEY, 194, 1), (a.EV_KEY, 194, 0)])
        m.update({"J": 0.0}, 0.01)
        m.macro_thread.join(2)
        m.update({"J": 3.5}, 0.01)
        self.assertEqual(drain(fd), [(a.EV_KEY, 194, 1), (a.EV_KEY, 194, 0)])
        os.close(fd)


class Toggle(unittest.TestCase):
    def test_axis_rules(self):
        self.assertEqual((a.Mapping("A=axis:3").kind, a.Mapping("A=axis:3").target), ("axis", 3))
        self.assertEqual((a.Mapping("A=axis:3+").target, a.Mapping("A=axis:3+").sign, a.Mapping("A=axis:3-").sign), (3, 1, -1))
        for bad in ("A=axis:0", "A=axis:65", "A=axis:x", "A=axis:3+-"):
            with self.assertRaises(ValueError):
                a.Mapping(bad)

    def test_ss_and_the_toggle_key_turn_mapping_off_and_on(self):
        m = a.Mapper(["J=key:F13"], swallow=False, pause_key=None, toggle="G")
        self.assertIn("SS", m.keys)
        self.assertIn("G", m.keys)
        self.assertNotIn("G", m.held)  # the toggle key itself keeps typing
        m.update({"G": 3.5}, 0.01)  # without SS it's just G
        self.assertFalse(m.paused)
        m.update({"SS": 2.0, "G": 3.5}, 0.01)
        self.assertTrue(m.paused)
        m.update({"SS": 2.0, "G": 3.5}, 0.01)  # still held: no second flip
        self.assertTrue(m.paused)
        m.update({"SS": 2.0}, 0.01)
        m.update({"SS": 2.0, "G": 3.5}, 0.01)
        self.assertFalse(m.paused)
        with self.assertRaises(ValueError):
            a.Mapper(["J=key:F13"], swallow=False, pause_key=None, toggle="Q")  # SS+Q is the keyboard's own

    def test_the_toggle_key_glows_its_state_while_ss_is_held(self):
        class Lights:
            def __init__(self):
                self.calls = []

            def set_colors(self, colors):
                self.calls.append(("set", colors))

            def clear_colors(self):
                self.calls.append(("clear",))

        m = a.Mapper(["J=key:F13"], swallow=False, pause_key=None, toggle="G")
        glow, kb = a.ToggleGlow(m, (0, 255, 0), (255, 0, 0)), Lights()
        glow.update(kb, {}, 0)
        self.assertEqual(kb.calls, [])
        glow.update(kb, {"SS": 2.0}, 0)
        glow.update(kb, {"SS": 2.0}, 0)  # sent once, not every frame
        self.assertEqual(kb.calls, [("set", {"G": (0, 255, 0)})])
        m.set_paused(True)
        glow.update(kb, {"SS": 2.0}, 0)
        self.assertEqual(kb.calls[-1], ("set", {"G": (255, 0, 0)}))
        glow.update(kb, {}, 0)
        self.assertEqual(kb.calls[-1], ("clear",))


class Shortcuts(unittest.TestCase):
    def mapper(self, *rules):
        m = a.Mapper(list(rules), swallow=True, pause_key=None)
        self.addCleanup(m.close)
        return m

    def runner(self):
        started = []
        real = a.subprocess.Popen

        class Done:
            def poll(self):
                return 0

        def popen(command, **kwargs):
            started.append(command)
            return Done()

        a.subprocess.Popen = popen
        self.addCleanup(setattr, a.subprocess, "Popen", real)
        return started

    def test_needs_the_steelseries_key_held(self):
        started = self.runner()
        m = self.mapper("SS+J=run:hello")
        m.update({"SS": 0.0, "J": 3.0}, 0.01)
        self.assertEqual(started, [])
        m.update({"SS": 0.0, "J": 0.0}, 0.01)
        m.update({"SS": 3.0, "J": 0.0}, 0.01)
        m.update({"SS": 3.0, "J": 3.0}, 0.01)
        self.assertEqual(started, ["hello"])

    def test_fires_once_per_press(self):
        started = self.runner()
        m = self.mapper("SS+J=run:hello")
        for _ in range(50):
            m.update({"SS": 3.0, "J": 3.0}, 0.01)
        self.assertEqual(started, ["hello"])
        m.update({"SS": 3.0, "J": 0.0}, 0.01)
        m.update({"SS": 3.0, "J": 3.0}, 0.01)
        self.assertEqual(started, ["hello", "hello"])

    def test_a_key_already_down_doesnt_count(self):
        started = self.runner()
        m = self.mapper("SS+J=run:hello")
        m.update({"SS": 0.0, "J": 3.0}, 0.01)  # typing J
        m.update({"SS": 3.0, "J": 3.0}, 0.01)  # then SS, with J still down
        self.assertEqual(started, [])
        m.update({"SS": 3.0, "J": 0.0}, 0.01)
        m.update({"SS": 3.0, "J": 3.0}, 0.01)
        self.assertEqual(started, ["hello"])

    def tap(self, m, held=0.1, others=None, step=0.01):
        frames = max(1, round(held / step))
        for _ in range(frames):
            m.update({"SS": 3.0, **(others or {})}, step)
        m.update({"SS": 0.0}, step)

    def test_a_quick_tap_of_ss_alone_fires(self):
        started = self.runner()
        m = self.mapper("SS=run:hello", "SS+J=run:there")
        self.assertFalse(m.want_swallow)
        self.assertEqual(m.held_keys(), [])
        self.assertEqual(m.read_keys(), ["J", "SS"])
        m.update({"SS": 3.0}, 0.01)
        self.assertIsNone(m.read_keys())  # every key while SS is down, to see a combo
        m.update({"SS": 0.0}, 0.01)
        self.assertEqual(m.read_keys(), ["J", "SS"])
        self.assertEqual(started, ["hello"])

    def test_holding_ss_or_using_it_for_a_combo_doesnt_fire(self):
        started = self.runner()
        m = self.mapper("SS=run:hello", "SS+J=run:there")
        self.tap(m, held=0.6)
        self.assertEqual(started, [])
        self.tap(m, others={"J": 3.0})
        self.assertEqual(started, ["there"])
        self.tap(m, others={"Q": 3.0})  # the keyboard's own SS+Q: not a shortcut here, but still a combo
        self.assertEqual(started, ["there"])
        self.tap(m)
        self.assertEqual(started, ["there", "hello"])

    def test_losing_the_keyboard_mid_press_isnt_a_tap(self):
        started = self.runner()
        m = self.mapper("SS=run:hello")
        m.update({"SS": 3.0}, 0.01)
        m.release()
        self.assertEqual(started, [])

    def test_a_tap_only_sends_one_off_things(self):
        for rule in ("SS=rt", "SS=lx-", "SS=mouse-x+", "SS=tap:RIGHT", "SS=clutch"):
            with self.assertRaisesRegex(ValueError, "tap of the SteelSeries key"):
                a.Mapping(rule)
        a.Mapping("SS=key:F13")
        a.Mapping("SS=volume+")

    def test_shortcut_rule_for_ss_is_a_tap(self):
        self.assertEqual(a.shortcut_rule("SS", "volume+"), "SS=volume+")
        self.assertEqual(a.shortcut_rule("J", "volume+"), "SS+J=volume+")
        self.assertTrue(a.Mapping(a.shortcut_rule("SS", "volume+")).ss_tap)

    def test_never_grabs_the_keyboard(self):
        m = self.mapper("SS+J=run:hello")
        self.assertFalse(m.want_swallow)
        self.assertEqual(m.held_keys(), [])
        self.assertEqual(set(m.keys), {"SS", "J"})

    def test_a_normal_rule_alongside_still_holds_only_its_own_key(self):
        m = self.mapper("SS+J=run:hello", "K=run:there")
        self.assertTrue(m.want_swallow)
        self.assertEqual(m.held_keys(), ["K"])

    def test_the_keyboards_own_combos_are_refused(self):
        for key in ("Q", "I", "O", "T", "LGUI", "SS"):
            with self.assertRaisesRegex(ValueError, "belongs to the keyboard"):
                a.Mapping(f"SS+{key}=run:x")

    def test_the_equals_key_and_lower_case(self):
        self.assertEqual(a.Mapping("SS+==run:x").key, "=")
        self.assertEqual(a.Mapping("ss+j=run:x").chord, "SS")
        self.assertEqual(a.Mapping("SS+J=run:x").text, "SS+J=run:x")

    def test_plain_rules_are_unchanged(self):
        rule = a.Mapping("J=run:x")
        self.assertIsNone(rule.chord)
        self.assertIsNone(rule.digital)

    def saved_config(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        saved = os.environ.get("XDG_CONFIG_HOME")
        os.environ["XDG_CONFIG_HOME"] = folder.name
        self.addCleanup(lambda: os.environ.pop("XDG_CONFIG_HOME") if saved is None else os.environ.__setitem__("XDG_CONFIG_HOME", saved))
        applied = []
        real = a._apply_shortcuts
        a._apply_shortcuts = applied.append
        self.addCleanup(setattr, a, "_apply_shortcuts", real)
        return applied

    def command(self, *argv):
        args = a.build_parser().parse_args(["shortcut", *argv])
        with contextlib.redirect_stdout(io.StringIO()) as out:
            args.func(args)
        return out.getvalue()

    def test_add_list_remove(self):
        applied = self.saved_config()
        self.command("add", "j", "volume+")
        self.assertEqual(json.loads(self.command("list", "--json")), {"J": "volume+"})
        self.assertEqual(applied[-1], {"J": "volume+"})
        self.command("remove", "J")
        self.assertEqual(json.loads(self.command("list", "--json")), {})
        self.assertEqual(applied[-1], {})

    def test_bad_rules_arent_saved(self):
        self.saved_config()
        for key, output in (("Q", "volume+"), ("J", "nonsense")):
            with self.assertRaises(ValueError):
                self.command("add", key, output)
        self.assertEqual(json.loads(self.command("list", "--json")), {})

    def test_punctuation_keys(self):
        self.saved_config()
        self.command("add", "COMMA", "volume-")
        self.assertEqual(json.loads(self.command("list", "--json")), {",": "volume-"})

    def test_daemon_brings_them_back_unless_turned_off(self):
        with tempfile.TemporaryDirectory() as folder:
            started = []
            daemon = a.Daemon.__new__(a.Daemon)
            daemon.state_path = os.path.join(folder, "daemon.json")
            daemon.start_job = lambda slot, *_: started.append(slot)
            a._write_private(daemon.state_path, {"lights": None, "recording": False})
            daemon.start_saved_jobs()
            self.assertEqual(started, [])
            a._write_private(os.path.join(folder, "shortcuts.json"), {"J": "volume+"})
            daemon.start_saved_jobs()
            self.assertEqual(started, ["shortcuts"])
            started.clear()
            a._write_private(daemon.state_path, {"lights": None, "recording": False, "shortcuts": False})
            daemon.start_saved_jobs()
            self.assertEqual(started, [])


class Oled(unittest.TestCase):
    def test_page_major_bit_layout(self):
        frame = a.OledFrame()
        frame.pixel(3, 0)
        frame.pixel(3, 9)
        self.assertEqual(frame.data[3], 1)
        self.assertEqual(frame.data[128 + 3], 2)
        self.assertEqual(len(frame.data), 640)
        frame.pixel(500, 500)
        self.assertEqual(len(frame.data), 640)

    def test_font_covers_every_sensor_key_name(self):
        for name in a.BY_NAME:
            for character in name:
                self.assertIn(character, a.FONT, name)
        self.assertTrue(all(len(v) == 10 for v in a.FONT.values()))

    def test_depth_frame_bar_follows_depth(self):
        half = a.oled_depth_frame("W", 2.0, 1.0)
        self.assertTrue(half.is_set(10, 33))
        self.assertFalse(half.is_set(100, 33))
        self.assertTrue(a.oled_depth_frame("W", 4.0).is_set(120, 33))

    def test_map_fits_the_screen_including_the_bottom_row(self):
        frame = a.oled_map_frame({k: 4.0 for k in a.POSITIONS})
        for key, (x, y) in a.POSITIONS.items():
            left, top = int(x * 6.9), int(y * 6.2)
            self.assertLessEqual(left + 5, a.OLED_WIDTH, key)
            self.assertLessEqual(top + 6, a.OLED_HEIGHT, key)
            self.assertTrue(frame.is_set(left + 1, top + 3), key)

    def test_map_shows_pressed_key(self):
        idle = a.oled_map_frame({"W": 0.0})
        pressed = a.oled_map_frame({"W": 4.0})
        self.assertGreater(sum(bin(b).count("1") for b in pressed.data), sum(bin(b).count("1") for b in idle.data))

    def test_frame_reaches_the_daemon_in_its_format(self):
        import base64
        import json
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "s")
            server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            self.addCleanup(server.close)
            server.bind(path)
            server.listen(1)
            frame = a.oled_depth_frame("W", 1.0)
            client_thread = threading.Thread(target=a.oled_send, args=(frame, 0.6, path))
            client_thread.start()
            connection, _ = server.accept()
            with connection:
                line = b""
                while not line.endswith(b"\n"):
                    line += connection.recv(4096)
                connection.sendall(b'{"ok": true}\n')
            client_thread.join(2)
            message = json.loads(line)
            self.assertEqual(message["cmd"], "notify")
            self.assertEqual(base64.b64decode(message["fb"]), bytes(frame.data))



class FakeDevice:
    firmware, node = "0416", "/dev/fake"

    def __init__(self):
        self.asked, self.written, self.closed = [], [], False
        self.fail = None

    def exchange_once(self, payload):
        if self.fail:
            raise self.fail
        self.asked.append(payload)
        if payload[0] == 0xD7:
            return struct.pack("<28H", *[850 + payload[1]] * 28)
        return bytes([payload[0]]) + bytes(63)

    def _write(self, payload):
        if self.fail:
            raise self.fail
        self.written.append(payload)

    def close(self):
        self.closed = True


class DaemonBroker(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.path = os.path.join(self.folder.name, "s")
        self.device = FakeDevice()
        sleeper = [sys.executable, "-c", "import sys, time; print('started'); sys.stdout.flush(); time.sleep(30)"]
        self.daemon = a.Daemon(self.path, lambda: self.device, lambda argv: sleeper, os.path.join(self.folder.name, "state.json"))
        self.assertTrue(self.daemon.listen())
        self.addCleanup(self.daemon.shutdown)
        self.client = a.DaemonClient(self.path)
        self.addCleanup(self.client.close)

    def test_each_program_gets_its_own_answer(self):
        other = a.DaemonClient(self.path)
        self.addCleanup(other.close)
        self.assertEqual(self.client.call(a.OP_ASK, bytes([0xD7, 1])), struct.pack("<28H", *[851] * 28))
        self.assertEqual(other.call(a.OP_ASK, bytes([0xD7, 2])), struct.pack("<28H", *[852] * 28))
        self.assertEqual(self.client.call(a.OP_ASK, bytes([0xD7, 1]))[:2], struct.pack("<H", 851))

    def test_close_together_reads_share_one_real_read(self):
        self.daemon.READ_CACHE = 5.0
        for _ in range(5):
            self.client.call(a.OP_ASK, bytes([0xD7, 3]))
        self.assertEqual(len(self.device.asked), 1)
        self.client.call(a.OP_ASK, bytes([0xDA, 1]))
        self.client.call(a.OP_ASK, bytes([0xDA, 1]))
        self.assertEqual(len(self.device.asked), 3)

    def test_a_write_clears_shared_reads(self):
        self.daemon.READ_CACHE = 5.0
        self.client.call(a.OP_ASK, bytes([0xD7, 3]))
        self.client.call(a.OP_WRITE, bytes([0x3B]))
        self.client.call(a.OP_ASK, bytes([0xD7, 3]))
        self.assertEqual(len(self.device.asked), 2)
        self.assertEqual(self.device.written, [[0x3B]])

    def test_daemon_also_refuses_unknown_reads(self):
        for command in (0x01, 0x81, 0x7F, 0x3B):
            with self.assertRaisesRegex(a.ApexError, "refused"):
                self.client.call(a.OP_ASK, bytes([command]))
        self.assertEqual(self.device.asked, [])

    def test_real_keyboard_class_works_through_the_daemon(self):
        saved = os.environ.get("XDG_RUNTIME_DIR")
        os.environ["XDG_RUNTIME_DIR"] = self.folder.name
        self.addCleanup(lambda: os.environ.__setitem__("XDG_RUNTIME_DIR", saved) if saved else os.environ.pop("XDG_RUNTIME_DIR"))
        os.rename(self.path, a._socket_path())
        self.daemon.path = a._socket_path()

        class Calibrated(FakeDevice):
            def exchange_once(inner, payload):
                if payload[0] == 0xDA:
                    out = b"".join(struct.pack("<BHH", payload[2 + i * 5], 3400, 850) for i in range(payload[1]))
                    return out.ljust(64, b"\0")
                return FakeDevice.exchange_once(inner, payload)
        self.device = Calibrated()
        with a.Keyboard() as kb:
            self.assertIsNotNone(kb.client)
            self.assertEqual(kb.firmware, "0416")
            self.assertEqual(kb.raw("W")["W"], 852)
            kb.set_colors({"W": (1, 2, 3)})
            self.assertEqual(self.device.written[-1][:6], [0x3A, 1, 0x1A, 1, 2, 3])

    def test_a_lost_keyboard_comes_back_as_disconnected_then_recovers(self):
        self.client.call(a.OP_OPEN)
        self.device.fail = a.Disconnected("unplugged")
        with self.assertRaises(a.Disconnected):
            self.client.call(a.OP_ASK, bytes([0xDA]))
        self.assertTrue(self.device.closed)
        self.device.fail, self.device.closed = None, False
        self.assertTrue(self.client.call(a.OP_ASK, bytes([0xDA])))

    def test_a_missing_keyboard_is_reported_as_missing(self):
        def missing():
            raise a.NotFound("No keyboard")
        self.daemon.open_keyboard = missing
        with self.assertRaisesRegex(a.NotFound, "No keyboard"):
            self.client.call(a.OP_OPEN)

    def job(self, slot="lights", argv=("lights", "--keep")):
        return a._job_call(self.client, a.OP_JOB, {"slot": slot, "label": argv[0], "argv": list(argv)})

    def test_starting_a_job_replaces_the_one_in_its_slot_and_is_remembered(self):
        self.assertTrue(self.job()["ok"])
        first = self.daemon.jobs["lights"]["process"]
        self.assertTrue(self.job(argv=("lights", "--effect", "ripple"))["ok"])
        self.assertIsNotNone(first.poll())
        self.assertEqual(self.daemon._state()["lights"], ["lights", "--effect", "ripple"])
        status = a._job_call(self.client, a.OP_STATUS, b"")
        self.assertTrue(status["jobs"]["lights"]["running"])

    def test_jobs_in_different_slots_run_together(self):
        self.job()
        self.job("input", ("gamepad",))
        self.assertIsNone(self.daemon.jobs["lights"]["process"].poll())
        self.assertIsNone(self.daemon.jobs["input"]["process"].poll())
        self.assertNotIn("input", self.daemon._state())

    def test_off_stops_a_job_and_forgets_it(self):
        self.job()
        process = self.daemon.jobs["lights"]["process"]
        self.assertTrue(a._job_call(self.client, a.OP_OFF, b"lights")["found"])
        self.assertIsNotNone(process.poll())
        self.assertIsNone(self.daemon._state()["lights"])
        self.assertFalse(a._job_call(self.client, a.OP_OFF, b"lights")["found"])

    def test_a_job_that_fails_straight_away_reports_why(self):
        self.daemon.spawn = lambda argv: [sys.executable, "-c", "import sys; print('apex-analog: bad rule'); sys.exit(1)"]
        result = self.job("input", ("map", "W=nope"))
        self.assertFalse(result["ok"])
        self.assertIn("bad rule", result["log"])

    def test_login_brings_back_lights_and_recording_but_never_a_gamepad(self):
        self.daemon.start_saved_jobs()
        self.assertEqual(sorted(self.daemon.jobs), ["lights", "recording"])
        self.assertEqual(self.daemon.jobs["lights"]["argv"], a.DEFAULT_LIGHTS)
        self.daemon._remember(lights=None, recording=False)
        for slot in list(self.daemon.jobs):
            self.daemon.stop_job(slot)
        self.daemon.start_saved_jobs()
        self.assertEqual(self.daemon.jobs, {})

    def test_second_daemon_backs_off_and_a_stale_socket_is_replaced(self):
        self.assertFalse(a.Daemon(self.path).listen())
        stale = os.path.join(self.folder.name, "stale")
        leftover = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        leftover.bind(stale)
        leftover.close()
        fresh = a.Daemon(stale, state_path=os.path.join(self.folder.name, "st2.json"))
        self.assertTrue(fresh.listen())
        fresh.shutdown()

    def test_socket_is_private_to_this_user(self):
        self.assertEqual(os.stat(self.path).st_mode & 0o777, 0o600)

    def test_which_commands_leave_the_terminal(self):
        parse = a.build_parser().parse_args
        self.assertTrue(a._runs_as_a_job(parse(["gamepad"])))
        self.assertTrue(a._runs_as_a_job(parse(["lights", "--effect", "ripple"])))
        self.assertFalse(a._runs_as_a_job(parse(["gamepad", "--watch"])))
        self.assertFalse(a._runs_as_a_job(parse(["lights", "--off"])))
        self.assertFalse(a._runs_as_a_job(parse(["stream"])))
        self.assertTrue(a._runs_as_a_job(parse(["stream", "--keys", "W", "--udp", "127.0.0.1:9"])))
        self.assertFalse(a._runs_as_a_job(parse(["info"])))
        self.assertFalse(a._runs_as_a_job(parse(["bars"])))


class HelpAndBars(unittest.TestCase):
    def test_help_lists_every_user_command(self):
        for name, sub in a.build_parser().command_parsers.items():
            if name not in ("record", "daemon", "help"):
                self.assertRegex(a.HELP_TEXT, rf"\b{name}\b", name)

    def test_help_on_one_command(self):
        import contextlib
        import io
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            a.cmd_help(a.build_parser().parse_args(["help", "gamepad"]))
        self.assertIn("--deadzone", out.getvalue())
        with self.assertRaisesRegex(ValueError, "No command"):
            a.cmd_help(a.build_parser().parse_args(["help", "nope"]))

    def settings(self, names):
        return {n: {"press": 1.89} for n in names}

    def test_bars_show_every_pressed_key_when_they_fit(self):
        names = list(a.BY_NAME)[:20]
        lines = a.bar_lines([(2.0, n) for n in names], self.settings(names), 120, 30)
        self.assertEqual(len(lines), 20)
        self.assertTrue(all(any(l.startswith(n) for l in lines) for n in names))

    def test_bars_use_columns_rather_than_dropping_keys(self):
        names = [n for n in a.BY_NAME]
        lines = a.bar_lines([(2.0, n) for n in names], self.settings(names), 200, 20)
        self.assertLessEqual(len(lines), 20)
        text = "\n".join(lines)
        for name in names:
            self.assertIn(f"{name:7}|", text)
        self.assertTrue(all(len(l) <= 200 for l in lines))

    def test_no_keys_pressed(self):
        self.assertEqual(a.bar_lines([], {}, 80, 20), ["Press a key slowly."])


class Packaging(unittest.TestCase):
    def test_pyproject_and_module_agree_on_the_version(self):
        with open(os.path.join(os.path.dirname(__file__), "pyproject.toml")) as f:
            declared = re.search(r'^version = "([^"]+)"', f.read(), re.M)
        self.assertIsNotNone(declared, "pyproject.toml has no version")
        self.assertEqual(declared.group(1), a.__version__)


if __name__ == "__main__":
    unittest.main()
