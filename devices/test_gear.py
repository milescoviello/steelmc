#!/usr/bin/env python3
"""Tests for the general device engine. No device needed."""
import contextlib
import io
import json
import os
import socket
import tempfile
import unittest
from unittest import mock

import gear
import steelmc_devices as sd

FAMILIES = gear.load_families()


def family(pid):
    return FAMILIES[pid][0]


class Definitions(unittest.TestCase):
    def test_every_default_encodes(self):
        for pid, (fam, _) in FAMILIES.items():
            for name, s in fam["settings"].items():
                if s["type"].startswith("unsupported"):
                    continue
                value = s.get("default") if s["type"] != "action" else None
                if value is None and s["type"] not in ("action",):
                    continue
                with self.subTest(pid=pid, setting=name):
                    data = gear.frame(s, value if s["type"] != "dpi_stages" else value)
                    self.assertEqual(data[:len(s["command"])], s["command"])
                    if s.get("packet_length"):
                        self.assertEqual(len(data), s["packet_length"])

    def test_dedicated_tools_keep_their_devices(self):
        covered = sd.families()
        for pid in sd.DEDICATED:
            self.assertNotIn(pid, covered)
        self.assertIn("1824", covered)  # Rival 3

    def test_udev_rule_matches_the_definitions(self):
        with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "udev", "70-steelmc-devices.rules")) as f:
            self.assertEqual(f.read(), sd.udev_rules(), "regenerate it: steelmc-devices udev-rules > udev/70-steelmc-devices.rules")

    def test_every_product_is_in_the_catalog(self):
        catalog = sd.load_catalog()
        missing = [pid for pid in FAMILIES if pid not in catalog]
        self.assertEqual(missing, [])


class Encoding(unittest.TestCase):
    def test_choice_number_and_colour(self):
        rival3 = family("1824")["settings"]
        self.assertEqual(gear.frame(rival3["polling_rate"], "1000"), [0x04, 0x00, 0x01])
        self.assertEqual(gear.frame(rival3["logo_color"], "ff8000"), [0x05, 0x00, 0x04, 0xFF, 0x80, 0x00, 0x64])
        self.assertEqual(gear.frame(rival3["logo_color"], "red")[3:6], [0xFF, 0, 0])
        with self.assertRaises(gear.Refused):
            gear.frame(rival3["polling_rate"], "2000")
        with self.assertRaises(gear.Refused):
            gear.frame(rival3["logo_color"], "reddish")

    def test_dpi_stages(self):
        s = family("1824")["settings"]["sensitivity"]
        data = gear.frame(s, "800,1600")
        self.assertEqual(data[:4], [0x0B, 0x00, 2, 1])
        self.assertEqual(len(data), 4 + 2 * s["bytes"])
        with self.assertRaises(gear.Refused):
            gear.frame(s, "800,1600,2400,3200,4000,5000")
        with self.assertRaises(gear.Refused):
            gear.frame(s, "90000")

    def test_number_with_a_wider_output(self):
        s = family("183a")["settings"]["sleep_timer"]  # minutes in, milliseconds out, 3 bytes
        self.assertEqual(gear.frame(s, "2")[-3:], [0xC0, 0xD4, 0x01])  # 120000 ms
        with self.assertRaises(gear.Refused):
            gear.frame(s, "21")

    def test_buttons(self):
        s = family("1824")["settings"]["buttons_mapping"]
        default = gear.frame(s, "default")
        swapped = gear.frame(s, "button1=button2,button2=button1")
        self.assertEqual(len(default), len(swapped))
        self.assertNotEqual(default, swapped)
        self.assertNotEqual(gear.frame(s, "button4=a"), default)
        with self.assertRaises(gear.Refused):
            gear.frame(s, "button9=button1")
        with self.assertRaises(gear.Refused):
            gear.frame(s, "button1=launch-rockets")

    def test_gradients(self):
        fam = family("1720") if "1720" in FAMILIES else next(f for f, _ in FAMILIES.values() if any(x["type"] == "gradient" for x in f["settings"].values()))
        s = next(x for x in fam["settings"].values() if x["type"] == "gradient")
        one = gear.frame(s, "ff0000")
        many = gear.frame(s, "2000ms:red@0,blue@50,red@100")
        self.assertGreater(len(many), len(one))
        fam2 = next(f for f, _ in FAMILIES.values() if any(x["type"] == "gradient2" for x in f["settings"].values()))
        s2 = next(x for x in fam2["settings"].values() if x["type"] == "gradient2")
        self.assertTrue(gear.frame(s2, "1000ms:red@0,blue@50,green@100"))
        with self.assertRaises(gear.Refused):
            gear.frame(s2, "10ms:red@0,blue@50,green@100")


class Talking(unittest.TestCase):
    def setUp(self):
        self.ours, self.device = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        self.addCleanup(self.ours.close)
        self.addCleanup(self.device.close)
        self.sent = []

        def write(fd, data):
            self.sent.append(bytes(data))
            if data[1] in (0xD2, 0x92):  # battery: level 15 of 21, charging
                self.device.send(bytes([data[1], 0x80 | 15]).ljust(64, b"\0"))
            elif self.answer_writes:
                self.device.send(bytes([data[1], 0]).ljust(64, b"\0"))
            return len(data)

        self.answer_writes = False
        self.write = write
        patcher = mock.patch("gear.time.sleep", lambda *_: None)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.config = tempfile.TemporaryDirectory()
        self.addCleanup(self.config.cleanup)
        patcher = mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": self.config.name})
        patcher.start()
        self.addCleanup(patcher.stop)

    def open(self, pid):
        g = gear.Gear(pid, FAMILIES, fd=self.ours.fileno())
        g.write = self.write
        return g

    def test_set_sends_the_frame_and_remembers_it(self):
        g = self.open("1824")
        g.set("polling_rate", "500")
        self.assertEqual(self.sent, [bytes([0, 0x04, 0x00, 0x02])])
        self.assertEqual(gear.remembered("1824"), {"polling_rate": "500"})

    def test_wireless_waits_for_its_answer_and_reads_battery(self):
        self.answer_writes = True
        g = self.open("1838")  # Aerox 3 Wireless, through its dongle
        g.set("polling_rate", "1000")
        self.assertEqual(self.sent[0][1] & 0x40, 0x40)
        self.assertEqual(g.battery(), {"percent": 70, "charging": True})

    def test_save_is_padded_where_needed(self):
        fam = next(f for f, _ in FAMILIES.values() if f.get("save", {}).get("packet_length"))
        pid = next(p for p, (f, _) in FAMILIES.items() if f is fam)
        self.open(pid).save()
        self.assertEqual(len(self.sent[-1]), 1 + fam["save"]["packet_length"])

    def test_nothing_unlisted_is_sent(self):
        g = self.open("1824")
        with self.assertRaises(gear.Refused):
            g.set("self_destruct", "now")
        self.assertEqual(self.sent, [])


class Headsets(unittest.TestCase):
    def setUp(self):
        self.ours, self.device = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        self.addCleanup(self.ours.close)
        self.addCleanup(self.device.close)
        self.sent, self.replies = [], {}
        patcher = mock.patch("gear.time.sleep", lambda *_: None)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.config = tempfile.TemporaryDirectory()
        self.addCleanup(self.config.cleanup)
        env = mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": self.config.name})
        env.start()
        self.addCleanup(env.stop)

    def open(self, pid):
        g = gear.Gear(pid, FAMILIES, fd=self.ours.fileno())

        def write(fd, data):
            self.sent.append(bytes(data))
            reply = self.replies.get(bytes(data[:2]))
            if reply is not None:
                self.device.send(reply)
            return len(data)
        g.write = write
        return g

    def test_raw_frames_are_padded_and_followed_by_a_save(self):
        g = self.open("220e")  # Arctis 7+
        g.set("sidetone", "medium")
        self.assertEqual(self.sent[0], bytes([0x00, 0x39, 0x02]).ljust(64, b"\0"))
        self.assertEqual(self.sent[1], bytes([0x00, 0x09]).ljust(64, b"\0"))
        legacy = self.open("1260")  # Arctis 7, 31-byte packets on report 6
        legacy.set("sidetone", "off")
        self.assertEqual(self.sent[2], bytes([0x06, 0x35]).ljust(31, b"\0"))
        self.assertEqual(self.sent[3], bytes([0x06, 0x09]).ljust(31, b"\0"))

    def test_equalizer(self):
        s = family("2202")["settings"]["equalizer"]
        self.assertEqual(gear.frame(s, "flat"), [0x00, 0x33] + [0x14] * 10 + [0x00])
        self.assertEqual(gear.frame(s, "1,2,3,4,5,-1,-2,-3,-4,-5")[2:12], [21, 22, 23, 24, 25, 19, 18, 17, 16, 15])
        plus = family("220e")["settings"]["equalizer"]
        self.assertEqual(gear.frame(plus, "bass")[2:12], [0x1F, 0x20, 0x1A, 0x15, 0x15, 0x16, 0x16, 0x16, 0x16, 0x23])
        with self.assertRaises(gear.Refused):
            gear.frame(s, "1,2,3")
        with self.assertRaises(gear.Refused):
            gear.frame(s, "11,0,0,0,0,0,0,0,0,0")
        g = self.open("12e0")  # Nova Pro Wireless picks its custom preset first
        g.set("equalizer", "0,0,0,0,0,0,0,0,0,1")
        self.assertEqual(self.sent[0][:3], bytes([0x06, 0x2E, 0x04]))
        self.assertEqual(self.sent[1][:2], bytes([0x06, 0x33]))

    def test_status(self):
        self.replies[bytes([0x00, 0xB0])] = bytes([0xB0, 0, 3, 0x01, 70, 30]).ljust(128, b"\0")
        self.assertEqual(self.open("2202").status(), {"connected": True, "charging": True, "battery": 75, "chat": 30, "game": 70})
        self.assertEqual(self.open("22a1").status()["battery"], 3)  # newer firmware reports a percentage
        self.replies[bytes([0x00, 0xB0])] = bytes([0xB0, 0, 0, 0x00]).ljust(128, b"\0")
        self.assertFalse(self.open("2202").status()["connected"])
        self.replies[bytes([0x06, 0x18])] = bytes([0x06, 0x18, 55]).ljust(8, b"\0")
        self.replies[bytes([0x06, 0x24])] = bytes([0x06, 0x24, 0, 223]).ljust(8, b"\0")
        self.assertEqual(self.open("1260").status(), {"battery": 55, "game": 100, "chat": 50})
        self.replies[bytes([0x00, 0xB0])] = bytes([0xB0, 0, 0, 3, 1, 80, 40]).ljust(128, b"\0")
        self.assertEqual(self.open("230a").status(), {"left_battery": 80, "right_battery": None})


class Cli(unittest.TestCase):
    def run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = sd.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def test_settings_need_no_device(self):
        code, out, _ = self.run_cli("settings", "1824")
        self.assertEqual(code, 0)
        self.assertIn("polling_rate", out)
        self.assertIn("experimental", out)

    def test_dedicated_and_unknown(self):
        self.assertIn("use aerox", self.run_cli("settings", "1858")[2])
        self.assertIn("isn't covered", self.run_cli("settings", "9999")[2])


if __name__ == "__main__":
    unittest.main()
