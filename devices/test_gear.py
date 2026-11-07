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
