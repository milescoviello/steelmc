#!/usr/bin/env python3
"""Tests for steelmc-devices, against a made-up /sys."""
import contextlib
import io
import json
import os
import tempfile
import urllib.parse
import unittest

import steelmc_devices as sd

ARCTIS = bytes.fromhex("06c0ff0901a10106c1ff150026ff00750809f09540810209f19540910209f2960202b102c0")
MEDIA = bytes.fromhex("050c0901a101850115002601ff1900290175089501810209008102c0")


class FakeSys:
    def __init__(self, root):
        self.root = root
        os.makedirs(os.path.join(root, "usb"))

    def device(self, port, vendor, product, bcd, name=None, hid=()):
        path = os.path.join(self.root, "usb", port)
        os.makedirs(path)
        for attr, value in (("idVendor", vendor), ("idProduct", product), ("bcdDevice", bcd), ("product", name)):
            if value is not None:
                with open(os.path.join(path, attr), "w") as f:
                    f.write(value + "\n")
        for number, descriptor in enumerate(hid):
            intf = os.path.join(path, f"{port}:1.{number}")
            node = os.path.join(intf, "0003:1038:0001.0001", "hidraw", f"hidraw{number + 10}")
            os.makedirs(node)
            for attr, value in (("bInterfaceNumber", f"{number:02x}"), ("bInterfaceClass", "03"), ("bInterfaceSubClass", "00"), ("bInterfaceProtocol", "00")):
                with open(os.path.join(intf, attr), "w") as f:
                    f.write(value + "\n")
            with open(os.path.join(intf, "0003:1038:0001.0001", "report_descriptor"), "wb") as f:
                f.write(descriptor)
        return path


class Catalog(unittest.TestCase):
    def test_every_entry_is_complete(self):
        catalog = sd.load_catalog()
        self.assertGreater(len(catalog), 400)
        for pid, entry in catalog.items():
            self.assertRegex(pid, r"^[0-9a-f]{4}$")
            self.assertTrue(entry["name"], pid)
            self.assertIn(entry["kind"], {"keyboard", "mouse", "headset", "mousepad", "controller", "speakers", "microphone", "lighting", "accessory", "other"}, pid)
            self.assertIn(entry["part"], {"device", "bootloader", "dongle", "base station", "headset", "bluetooth", "audio"}, pid)

    def test_what_works_today(self):
        catalog = sd.load_catalog()
        full = {pid for pid, e in catalog.items() if e.get("support", {}).get("level") == "full"}
        self.assertEqual(full, {"1640", "1858", "185a", "12c2"})
        read_only = {pid for pid, e in catalog.items() if e.get("support", {}).get("level") == "read-only"}
        self.assertEqual(read_only, {"1874", "1876"})
        experimental = {pid for pid, e in catalog.items() if e.get("support", {}).get("level") == "experimental"}
        self.assertEqual(experimental, {"1610", "1614"})
        self.assertEqual(catalog["1641"]["part"], "bootloader")


class Scanning(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.sys = FakeSys(self.tmp.name)
        self.usb = os.path.join(self.tmp.name, "usb")

    def test_lists_only_steelseries(self):
        self.sys.device("3-8.2", "1038", "12c2", "0101", "SteelSeries Arctis 9", hid=[ARCTIS, MEDIA])
        self.sys.device("3-1", "046d", "c52b", "1211", "Someone else's receiver")
        self.sys.device("3-2", "1038", "9999", "0100")
        found = sd.scan(self.usb)
        found = sorted(found, key=lambda d: d["id"])
        self.assertEqual([d["id"] for d in found], ["1038:12c2", "1038:9999"])
        self.assertEqual(found[0]["tools"], ["arctis"])
        self.assertFalse(found[1]["known"])
        self.assertIn("not in the catalog", sd.describe(found[1]))

    def test_a_headset_half_points_to_its_transmitter(self):
        self.sys.device("3-4", "1038", "2230", "0100", "Arctis Nova 5")  # the headset side; GG pairs it with the 2232 transmitter
        found = sd.scan(self.usb)
        self.assertEqual(found[0]["support"], "paired")
        self.assertEqual(found[0]["tools"], ["1038:2232"])
        self.assertIn("settings go through the transmitter", sd.describe(found[0]))

    def test_probe_decodes_each_interface(self):
        path = self.sys.device("3-8.2", "1038", "12c2", "0101", "SteelSeries Arctis 9", hid=[ARCTIS, MEDIA])
        first, second = sd.interfaces(path)
        self.assertEqual(first["hid"][0]["usage_pages"], ["ffc0", "ffc1"])
        self.assertTrue(first["hid"][0]["vendor_defined"])
        self.assertEqual(first["hid"][0]["reports"], [{"id": 0, "kind": "feature", "bytes": 514}, {"id": 0, "kind": "input", "bytes": 64},
                                                      {"id": 0, "kind": "output", "bytes": 64}])
        self.assertEqual(second["hid"][0]["reports"], [{"id": 1, "kind": "input", "bytes": 2}])
        report = sd.probe_report(sd.scan(self.usb)[0])
        self.assertIn("interface 00", report)
        self.assertIn(ARCTIS.hex(), report)

    def test_a_report_says_what_it_is_and_links_to_a_filled_in_issue(self):
        self.sys.device("3-8.2", "1038", "12c2", "0101", "SteelSeries Arctis 9", hid=[ARCTIS, MEDIA])
        release = os.path.join(self.tmp.name, "os-release")
        with open(release, "w") as f:
            f.write('NAME="Debian"\nPRETTY_NAME="Debian GNU/Linux 13 (trixie)"\n')
        device = sd.scan(self.usb)[0]
        text = sd.report(device, {"sidetone": "2"}, release)
        first = text.splitlines()[0]
        self.assertTrue(first.startswith(f"steelmc {sd.__version__}, Debian GNU/Linux 13 (trixie), kernel "))
        self.assertIn("interface 00", text)
        self.assertIn("Last set here: sidetone 2", text)
        self.assertNotIn(os.path.expanduser("~"), text)
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(sd.report_url(device, text)).query)
        self.assertEqual(query["template"], ["device.yml"])
        self.assertEqual(query["device"], ["SteelSeries Arctis 9 (1038:12c2)"])
        self.assertEqual(query["report"], [text])
        with open(release, "w") as f:
            f.write("PRETTY_NAME='Gentoo Linux'\n")
        self.assertIn(", Gentoo Linux, kernel", sd.system_line(release))

    def test_a_long_report_leaves_the_descriptors_out_of_the_link(self):
        self.sys.device("3-8.2", "1038", "12c2", "0101", "SteelSeries Arctis 9", hid=[ARCTIS * 60, MEDIA * 60])
        device = sd.scan(self.usb)[0]
        text = sd.report(device)
        url = sd.report_url(device, text)
        self.assertLessEqual(len(url), sd.URL_LIMIT)
        report = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)["report"][0]
        self.assertNotIn(ARCTIS.hex(), report)
        self.assertIn("interface 00", report)
        self.assertIn("didn't fit", report)

    def test_it_never_opens_a_device(self):
        self.sys.device("3-8.2", "1038", "12c2", "0101", hid=[ARCTIS])
        real = os.open
        opened = []
        os.open = lambda path, *a, **k: opened.append(path) or real(path, *a, **k)
        try:
            device = sd.scan(self.usb)[0]
            sd.probe_report(device)
        finally:
            os.open = real
        self.assertFalse([p for p in opened if p.startswith("/dev/")])

    def test_cli(self):
        out = io.StringIO()
        real = sd.scan
        sd.scan = lambda: real(self.usb)
        try:
            self.sys.device("3-8.2", "1038", "12c2", "0101", hid=[ARCTIS])
            with contextlib.redirect_stdout(out):
                self.assertEqual(sd.main(["--json"]), 0)
        finally:
            sd.scan = real
        self.assertEqual(json.loads(out.getvalue())[0]["id"], "1038:12c2")


if __name__ == "__main__":
    unittest.main()
