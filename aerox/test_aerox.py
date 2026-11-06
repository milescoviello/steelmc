#!/usr/bin/env python3
"""Tests for aerox. No hardware needed: replies come through a socket pair, and sample
replies are real ones captured from an Aerox 9 Wireless on firmware 1.2.4."""
import contextlib
import io
import json
import os
import select
import socket
import tempfile
import unittest
from unittest import mock

import aerox

# Things the firmware says must never be sent casually. Notes: ~/aerox-re/FINDINGS.md, kept out of this repo.
NEVER = {
    0x01, 0x41,              # reboot, maybe into the bootloader
    0x02, 0x42, 0x03, 0x43,  # erase and write the update partition
    0x11, 0x51,              # save to flash
    0x91, 0xD1,              # looks like a read, restores defaults and saves
    0x30, 0x31, 0x32, 0x39, 0x70, 0x71, 0x72, 0x79,  # identity data, 39 can lock it
    0x36, 0x37, 0x38, 0x76, 0x77, 0x78,  # radio test registers
    0x3A, 0x3B, 0x7A, 0x7B,  # radio link settings
    0x3F, 0x7F,              # erases a partition
    0x08, 0x48,              # erases a partition, resets the sensor
    0x24, 0x64,              # can restore lighting and save
    0x06, 0x46, 0x86, 0xC6,  # raw LED driver registers
    0x07, 0x47, 0x87, 0xC7,  # raw sensor registers
    0x83, 0xC3,              # stack overflow on a long length
    0x0A, 0x4A,              # sets three things at once
}


def reply(*data):
    return bytes(data).ljust(64, b"\0")


class Allowlist(unittest.TestCase):
    def test_nothing_dangerous_is_allowed(self):
        self.assertEqual(NEVER & set(aerox.ALLOWED), set())

    def test_only_the_allowed_bytes_pass(self):
        for cmd in range(256):
            if cmd not in aerox.ALLOWED:
                with self.assertRaises(aerox.Refused):
                    aerox.build_frame(cmd)

    def test_arguments_are_checked(self):
        for cmd, args in ((0xD2, (1,)), (0xD0, ()), (0xD0, (2,)), (0xEA, (3,)), (0xEA, (0, 0)), (0x90, (1, 1))):
            with self.assertRaises(aerox.Refused):
                aerox.build_frame(cmd, args)

    def test_frame_shape(self):
        frame = aerox.build_frame(0x90, (1,))
        self.assertEqual(len(frame), 65)
        self.assertEqual(frame[:3], b"\x00\x90\x01")
        self.assertEqual(frame[3:], bytes(62))


class FakeLink(unittest.TestCase):
    def setUp(self):
        self.ours, self.device = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        self.addCleanup(self.ours.close)
        self.addCleanup(self.device.close)
        self.mouse = aerox.Aerox("/dev/fake", "dongle", aerox.FIRMWARE_BCD, fd=self.ours.fileno())

    def answer(self, *replies):
        """Queue replies that arrive as soon as a frame is written."""
        real_write = os.write

        def write(fd, data):
            n = real_write(fd, data)
            for r in replies:
                self.device.send(r)
            return n

        patcher = mock.patch("aerox.os.write", write)
        patcher.start()
        self.addCleanup(patcher.stop)

    def sent(self):
        return self.device.recv(65) if select.select([self.device], [], [], 0)[0] else None

    def test_refused_commands_write_nothing(self):
        with self.assertRaises(aerox.Refused):
            self.mouse.request(0xD1, 0)
        self.assertIsNone(self.sent())

    def test_request_sends_the_frame(self):
        self.answer(reply(0xD2, 7))
        self.assertEqual(self.mouse.battery()["level"], 7)
        self.assertEqual(self.sent(), aerox.build_frame(0xD2))

    def test_stale_input_is_not_taken_as_the_answer(self):
        self.device.send(reply(0xD2, 99))
        self.answer(reply(0xD2, 7))
        self.assertEqual(self.mouse.battery()["level"], 7)

    def test_pushed_link_message_is_skipped(self):
        self.answer(reply(0xBC, 7), reply(0xBC, 1))
        self.assertTrue(self.mouse.link_up())

    def test_other_replies_are_skipped(self):
        self.answer(reply(0xEB, 0), reply(0xD2, 7))
        self.assertEqual(self.mouse.battery()["level"], 7)

    def test_dongle_cant_reach_mouse(self):
        self.answer(reply(0x40, 0xFF))
        with self.assertRaises(aerox.MouseUnreachable):
            self.mouse.request(0xD2)

    def test_no_reply_times_out(self):
        with self.assertRaises(aerox.MouseUnreachable):
            self.mouse.request(0xD2, timeout=0.01)

    def test_version_has_no_echo(self):
        self.answer(reply(*b"1.2.4"))
        self.assertEqual(self.mouse.string(0x90), "1.2.4")

    def test_mouse_on_other_firmware_stops(self):
        self.answer(reply(*b"1.2.5"))
        with self.assertRaisesRegex(aerox.AeroxError, "1.2.5"):
            self.mouse.check_mouse_firmware()

    def test_link_down_skips_mouse_reads(self):
        def write(fd, data, real=os.write):
            n = real(fd, data)
            self.device.send(reply(*b"1.2.4") if data[1] == 0x90 else reply(0xBC, 0))
            return n

        with mock.patch("aerox.os.write", write):
            status = aerox.collect_status(self.mouse)
        self.assertFalse(status["link"])
        self.assertNotIn("battery", status)


class Decoding(unittest.TestCase):
    def test_battery(self):
        self.assertEqual(aerox.decode_battery(reply(0xD2, 7)), {"level": 7, "percent": 30, "charging": False})
        self.assertEqual(aerox.decode_battery(reply(0xD2, 0x80 | 21)), {"level": 21, "percent": 100, "charging": True})

    def test_polling(self):
        self.assertEqual(aerox.decode_polling(reply(0xEB, 0))["hz"], 1000)
        self.assertEqual(aerox.decode_polling(reply(0xEB, 3))["hz"], 125)
        self.assertIsNone(aerox.decode_polling(reply(0xEB, 9))["hz"])

    def test_dpi(self):
        got = aerox.decode_dpi(reply(0xED, 5, 0, 0x04, 0x09, 0x0D, 0x1B, 0x26))
        self.assertEqual(got, {"count": 5, "current": 0, "values": [4, 9, 13, 27, 38]})

    def test_dpi_uses_count(self):
        self.assertEqual(aerox.decode_dpi(reply(0xED, 2, 1, 4, 9, 13, 27, 38))["values"], [4, 9])

    def test_default_buttons(self):
        buttons = aerox.decode_buttons(b"".join(aerox.DEFAULTS))
        self.assertEqual(len(buttons), 20)
        self.assertTrue(all(b["default"] for b in buttons))
        by_name = {b["name"]: b["action"] for b in buttons}
        self.assertEqual(by_name["left"], "mouse button 1")
        self.assertEqual(by_name["side 1"], "key 1")
        self.assertEqual(by_name["side 10"], "key 0")
        self.assertEqual(by_name["side 11"], "key -")
        self.assertEqual(by_name["side 12"], "key =")
        self.assertEqual(by_name["dpi button"], "next DPI stage")
        self.assertEqual(by_name["wheel up"], "scroll up")

    def test_actions(self):
        self.assertEqual(aerox.describe_action(bytes([0x51, 0xE0, 0x06, 0, 0])), "keys ctrl+c")
        self.assertEqual(aerox.describe_action(bytes([0x61, 0xE9, 0, 0, 0])), "media volume up")
        self.assertEqual(aerox.describe_action(bytes([0x62, 1, 2, 3, 4])), "unknown 62 01 02 03 04")
        self.assertEqual(aerox.describe_action(bytes(5)), "nothing")


class Finding(unittest.TestCase):
    def fake_sysfs(self, root, name, uevent, descriptor, bcd="0124"):
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
            self.fake_sysfs(root, "hidraw1", "00001038:00001640", aerox.DESCRIPTOR)  # keyboard
            self.fake_sysfs(root, "hidraw2", "00001038:00001858", b"\x05\x01\x09\x02")  # mouse motion
            sysfs = self.fake_sysfs(root, "hidraw8", "00001038:00001858", aerox.DESCRIPTOR)
            self.assertEqual(aerox.find_device(sysfs), ("/dev/hidraw8", "dongle", "0124"))

    def test_plugged_in_directly(self):
        with tempfile.TemporaryDirectory() as root:
            sysfs = self.fake_sysfs(root, "hidraw3", "00001038:0000185A", aerox.DESCRIPTOR)
            self.assertEqual(aerox.find_device(sysfs)[1], "wired")

    def test_nothing_connected(self):
        with tempfile.TemporaryDirectory() as root:
            with self.assertRaises(aerox.AeroxError):
                aerox.find_device(root)

    def test_other_firmware_is_refused(self):
        with self.assertRaisesRegex(aerox.AeroxError, "isn't 1.2.4"):
            aerox.Aerox("/dev/fake", "dongle", "0125", fd=-1)


class Command(unittest.TestCase):
    def test_lost_device_is_a_message_not_a_traceback(self):
        err = io.StringIO()
        with mock.patch("aerox.run", side_effect=OSError(19, "No such device")), contextlib.redirect_stderr(err):
            self.assertEqual(aerox.main(["battery"]), 1)
        self.assertIn("aerox:", err.getvalue())

    def test_status_json_is_valid(self):
        status = {"node": "/dev/x", "connection": "dongle", "link": False}
        out = io.StringIO()
        fake = mock.MagicMock()
        fake.__enter__.return_value = fake
        with mock.patch("aerox.find_device", return_value=("/dev/x", "dongle", "0124")), \
                mock.patch("aerox.Aerox", return_value=fake), \
                mock.patch("aerox.collect_status", return_value=status), \
                contextlib.redirect_stdout(out):
            self.assertEqual(aerox.main(["--json"]), 0)
        self.assertEqual(json.loads(out.getvalue()), status)


if __name__ == "__main__":
    unittest.main()
