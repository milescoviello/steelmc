#!/usr/bin/env python3
"""Tests for arctis. No headset needed: a simulated transmitter answers the way firmware 1.1 does, over a socket pair."""
import contextlib
import io
import json
import os
import socket
import struct
import tempfile
import unittest
from unittest import mock

import arctis

# What reading the firmware said must never be sent.
NEVER = {0x01, 0x02, 0x0C, 0x25, 0xFF, 0x08, 0x0A, 0x0E, 0x1A, 0x24, 0x2A, 0x2B, 0x2C, 0x0D}


def int8(db):
    return db & 0xFF


class SimulatedTransmitter:
    """Answers reads from a cleared 64-byte buffer, stores writes, and fails on anything it doesn't expect.
    With `noisy`, it sends one of its own status reports before every reply, as the real one can."""

    def __init__(self, sock):
        self.sock = sock
        self.frames = []
        self.noisy = False
        self.settings = {"sidetone": -18, "mic": 0, "surround": 0, "preset": 0, "bands": [0] * 10, "sleep": 180}
        self.connected = True
        self.saved = 0

    def push(self):
        # mode, rf status, mic mute, battery hi/lo, battery status, surround, chat, game
        self.sock.send(bytes([0, 1, 0, 0x0F, 0x6E, 1, 0, 64, 64]).ljust(64, b"\0"))

    def reply(self, data):
        if self.noisy:
            self.push()
        self.sock.send(bytes(data).ljust(64, b"\0"))

    def receive(self, frame):
        frame = bytes(frame)
        self.frames.append(frame)
        assert len(frame) == 65 and frame[0] == 0 and frame[2] == 0, frame.hex()
        cmd, args, s = frame[1], frame[3:], self.settings
        if cmd == 0x23:
            self.reply(b"\xaa\x55\x23\xdc")
        elif cmd == 0x10:
            self.reply(bytes([1, 1, 4, 91, 39, 70, 33, 0, 4, 1]) + b"0952" + b"0955" + bytes([2, 71, 0, 0, 0, 0]))
        elif cmd == 0x20:
            if not self.connected:
                self.reply(bytes([0xAA, 0, 0, 0, 0, 0, int8(s["mic"]), 1, int8(s["sidetone"]), 0, 0, 1]))
            else:
                self.reply(bytes([0xAA, 1, 0x0F, 0x6E, 1, 0, int8(s["mic"]), 1, int8(s["sidetone"]), 70, 30, 1]))
        elif cmd == 0x13:
            self.reply(struct.pack("<10i", *s["bands"]))
        elif cmd == 0x14:
            self.reply(bytes([s["sleep"] >> 8, s["sleep"] & 0xFF]))
        elif cmd == 0x15:
            self.reply(bytes([int8(s["mic"])]))
        elif cmd == 0x16:
            self.reply(bytes([int8(s["sidetone"])]))
        elif cmd == 0x17:
            self.reply(bytes([s["surround"]]))
        elif cmd == 0x1B:
            self.reply(bytes([s["preset"]]))
        elif cmd == 0x03:
            s["bands"] = list(struct.unpack("<10i", args[:40]))
        elif cmd == 0x04:
            s["sleep"] = args[0] << 8 | args[1]
        elif cmd == 0x05:
            s["mic"] = arctis._int8(args[0])
        elif cmd == 0x06:
            s["sidetone"] = arctis._int8(args[0])
        elif cmd == 0x07:
            if args[0] <= 1:
                s["surround"] = args[0]
        elif cmd == 0x0B:
            if args[0] <= 4:
                s["preset"] = args[0]
        elif cmd == 0x09:
            self.saved += 1
        else:
            raise AssertionError(f"unexpected command {frame.hex(' ')}")


class Harness(unittest.TestCase):
    def setUp(self):
        self.ours, self.device = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        self.addCleanup(self.ours.close)
        self.addCleanup(self.device.close)
        self.sim = SimulatedTransmitter(self.device)
        self.arctis = arctis.Arctis("/dev/fake", arctis.FIRMWARE_BCD, fd=self.ours.fileno())

        def write(fd, data):
            self.sim.receive(data)
            return len(data)

        for target, value in (("arctis.os.write", write), ("arctis.time.sleep", lambda *_: None)):
            patcher = mock.patch(target, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def cli(self, *argv, answer=None):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), mock.patch("builtins.input", lambda *_: answer or ""):
            code = arctis.run(arctis.parse_args(list(argv)), self.arctis)
        return code, out.getvalue()

    def sent(self):
        return [f[1] for f in self.sim.frames]


class Allowlist(unittest.TestCase):
    def test_every_command_byte(self):
        allowed = set(arctis.READS) | set(arctis.WRITES)
        for cmd in range(256):
            if cmd in allowed:
                continue
            with self.assertRaises(arctis.Refused, msg=hex(cmd)):
                arctis.build_frame(cmd, ())
        self.assertFalse(allowed & NEVER)

    def test_arguments(self):
        good = [(0x06, [int8(-18)]), (0x06, [int8(-64)]), (0x05, [int8(-25)]), (0x05, [0]), (0x07, [1]), (0x0B, [4]),
                (0x04, [0, 0]), (0x04, [0x15, 0x18]), (0x03, list(struct.pack("<10i", *[arctis.EQ_LIMIT] * 10))), (0x09, [])]
        for cmd, args in good:
            arctis.check_allowed(cmd, args)
        bad = [(0x06, [int8(-20)]), (0x06, []), (0x05, [int8(-26)]), (0x05, [1]), (0x07, [2]), (0x0B, [5]),
               (0x04, [0x15, 0x19]), (0x03, list(struct.pack("<10i", *[arctis.EQ_LIMIT + 1] * 10))), (0x03, [0] * 39),
               (0x09, [0]), (0x20, [1]), (0x16, [0])]
        for cmd, args in bad:
            with self.assertRaises(arctis.Refused, msg=f"{cmd:#04x} {args}"):
                arctis.check_allowed(cmd, args)

    def test_frame_has_the_selector(self):
        self.assertEqual(arctis.build_frame(0x06, [int8(-8)])[:5], bytes([0, 0x06, 0, int8(-8), 0]))
        self.assertEqual(len(arctis.build_frame(0x20)), 65)


class Reading(Harness):
    def test_status(self):
        self.assertEqual(self.arctis.status(), {
            "ready": True, "connected": True, "rf_status": 1, "battery_mv": 3950, "battery_status": 1, "mic_muted": False,
            "mic_db": 0, "sidetone_on": True, "sidetone_db": -18, "chat": 70, "game": 30, "mode": 1})
        self.sim.connected = False
        self.assertFalse(self.arctis.status()["connected"])

    def test_versions(self):
        self.assertEqual(self.arctis.versions(), {"transmitter": "1.1", "conexant": "4.91.39.70", "codec": "33.0", "headset": "1.4",
                                                  "radio_transmitter": "0952", "radio_headset": "0955", "bluetooth": "2.71"})

    def test_stray_status_reports_are_skipped(self):
        self.sim.noisy = True
        self.assertEqual(self.arctis.sidetone(), -18)
        self.assertEqual(self.arctis.surround(), False)
        self.assertEqual(self.arctis.eq_preset(), 0)
        self.assertEqual(self.arctis.sleep_seconds(), 180)
        self.assertEqual(self.arctis.status()["chat"], 70)

    def test_only_reads_on_status(self):
        code, said = self.cli()
        self.assertEqual(code, 0)
        self.assertIn("battery 3.95 V", said)
        self.assertIn("Sidetone: low", said)
        self.assertEqual(self.sent(), [0x23, 0x10, 0x20])
        self.assertTrue(all(cmd in arctis.READS for cmd in self.sent()))


class Settings(Harness):
    def test_each_setting_is_read_back(self):
        self.assertIn("Sidetone: high", self.cli("sidetone", "high")[1])
        self.assertEqual(self.sim.settings["sidetone"], -3)
        self.cli("mic", "-12")
        self.assertEqual(self.sim.settings["mic"], -12)
        self.cli("surround", "on")
        self.assertEqual(self.sim.settings["surround"], 1)
        self.cli("eq", "bass")
        self.assertEqual(self.sim.settings["preset"], 1)
        self.cli("eq", *["2"] * 5, *["-1.5"] * 5)
        self.assertEqual(self.sim.settings["bands"], [2 * arctis.EQ_UNIT] * 5 + [round(-1.5 * arctis.EQ_UNIT)] * 5)
        self.assertEqual(self.sim.settings["preset"], 4)
        self.assertIn("after 10 min", self.cli("sleep", "10")[1])
        self.assertEqual(self.sim.settings["sleep"], 600)
        self.assertEqual(self.sim.saved, 0)

    def test_out_of_range_never_reaches_it(self):
        for argv in (["mic", "3"], ["eq", "loud"], ["eq", "1", "2"], ["eq", *["11"] * 10], ["sleep", "91"]):
            before = len(self.sim.frames)
            with self.assertRaises(arctis.ArctisError, msg=argv):
                self.cli(*argv)
            self.assertTrue(all(f[1] in arctis.READS for f in self.sim.frames[before:]), argv)

    def test_save_asks_first(self):
        self.assertEqual(self.cli("save", answer="no")[0], 1)
        self.assertEqual(self.sim.saved, 0)
        self.cli("save", answer="yes")
        self.assertEqual(self.sim.saved, 1)
        self.cli("save", "--yes")
        self.assertEqual(self.sim.saved, 2)

    def test_json(self):
        _, said = self.cli("--json", "sidetone")
        self.assertEqual(json.loads(said), {"sidetone_db": -18, "level": "low"})


class Finding(unittest.TestCase):
    def fake_sysfs(self, root, name, uevent, descriptor, bcd="0101"):
        usb = os.path.join(root, "usb", name)
        hid = os.path.join(usb, "hid")
        os.makedirs(hid)
        with open(os.path.join(usb, "bcdDevice"), "w") as f:
            f.write(bcd + "\n")
        with open(os.path.join(hid, "uevent"), "w") as f:
            f.write(f"HID_ID=0003:{uevent}\n")
        with open(os.path.join(hid, "report_descriptor"), "wb") as f:
            f.write(descriptor)
        node = os.path.join(root, "sys", name)
        os.makedirs(node)
        os.symlink(hid, os.path.join(node, "device"))
        return os.path.join(root, "sys")

    def test_finds_only_the_vendor_interface(self):
        with tempfile.TemporaryDirectory() as root:
            self.fake_sysfs(root, "hidraw16", "00001038:000012C2", bytes.fromhex("050c0901a101"))  # media keys
            self.fake_sysfs(root, "hidraw19", "00001038:000012C4", arctis.DESCRIPTOR)  # the other device on the hub
            sysfs = self.fake_sysfs(root, "hidraw17", "00001038:000012C2", arctis.DESCRIPTOR)
            self.assertEqual(arctis.find_device(sysfs), ("/dev/hidraw17", "0101"))

    def test_nothing_connected(self):
        with tempfile.TemporaryDirectory() as root:
            with self.assertRaisesRegex(arctis.ArctisError, "No Arctis 9"):
                arctis.find_device(root)

    def test_other_firmware_is_refused(self):
        with self.assertRaisesRegex(arctis.ArctisError, "isn't 1.1"):
            arctis.Arctis("/dev/fake", "0102", fd=-1)


if __name__ == "__main__":
    unittest.main()
