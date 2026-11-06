#!/usr/bin/env python3
"""Tests for aerox. No hardware needed: a small simulated mouse answers over a socket pair,
and sample replies are real ones captured from an Aerox 9 Wireless on firmware 1.2.4."""
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
    0x11,                    # save, in the form that goes to the dongle alone
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
    0x60,                    # the lighting pattern: a bad one is what makes 64 01 reset and save
}


def reply(*data):
    return bytes(data).ljust(64, b"\0")


class SimulatedMouse:
    """Just enough of the real protocol, seen from the dongle's side."""

    def __init__(self, device):
        self.device = device
        self.link = True
        self.resync_reads = 0  # link checks that say "down" after a polling change
        self.polling = 0
        self.count, self.current, self.levels = 5, 0, [4, 9, 13, 27, 38]
        self.sleep = 300000
        self.low_power = 0
        self.light = [15, 1, 1, 0, *(30000).to_bytes(4, "little")]
        self.reactive = [1, 0, 255, 255, 255]
        self.records = bytearray(b"".join(aerox.DEFAULTS))
        self.battery = 5
        self.colors = None
        self.saved = []
        self.frames = []

    def answer(self, frame):
        assert len(frame) == 65 and frame[0] == 0
        cmd, *a = frame[1:]
        self.frames.append(frame)
        if cmd in (0x90, 0xD0):
            return reply(*(b"1.2.4" if a[0] == 0 else b"bf02fa4b"))
        if cmd == 0xBC:
            if self.resync_reads:
                self.resync_reads -= 1
                return reply(0xBC, 0)
            return reply(0xBC, int(self.link))
        if not self.link or (self.resync_reads and cmd & 0x40):
            return reply(0x40, 0xFF)
        if cmd == 0xD2:
            return reply(0xD2, self.battery)
        if cmd == 0xEB:
            return reply(0xEB, self.polling)
        if cmd == 0xED:
            return reply(0xED, self.count, self.current, *self.levels)
        if cmd == 0xE9:
            return reply(0xE9, *self.sleep.to_bytes(4, "little"))
        if cmd == 0xE8:
            return reply(0xE8, self.low_power)
        if cmd == 0xE3:
            return reply(0xE3, *self.light)
        if cmd == 0xE6:
            return reply(0xE6, *self.reactive)
        if cmd == 0xEA:
            span = {0: (0, 30), 1: (30, 60), 2: (60, 100)}[a[0]]
            return reply(0xEA, *self.records[span[0]:span[1]])
        if cmd == 0x6B:
            self.polling = a[0]
            self.resync_reads = 3
        elif cmd == 0x6D:
            self.count, self.current, self.levels = a[0], a[1], list(a[2:2 + a[0]])
        elif cmd == 0x6E:
            self.current = a[0]
        elif cmd == 0x69:
            self.sleep = int.from_bytes(bytes(a[:4]), "little")
        elif cmd == 0x68:
            self.low_power = a[0]
        elif cmd == 0x63:
            self.light = list(a[:8])
        elif cmd == 0x66:
            self.reactive = list(a[:5])
        elif cmd == 0x6A:
            start, size = {0: (0, 30), 1: (30, 30), 2: (60, 40)}[a[0]]
            self.records[start:start + size] = bytes(a[1:1 + size])
        elif cmd == 0x61:
            self.colors = list(a[:1 + a[0] * 4])
        elif cmd == 0x62:
            self.colors = None
        elif cmd == 0x51:
            self.saved.append(a[0])
        else:
            raise AssertionError(f"the tool sent {cmd:#04x}")
        return reply(cmd, 0)


class Allowlist(unittest.TestCase):
    def test_nothing_dangerous_is_allowed(self):
        self.assertEqual(NEVER & set(aerox.ALLOWED), set())

    def test_only_the_allowed_bytes_pass(self):
        for cmd in range(256):
            if cmd not in aerox.ALLOWED:
                with self.assertRaises(aerox.Refused):
                    aerox.build_frame(cmd)

    def test_read_arguments_are_checked(self):
        for cmd, args in ((0xD2, (1,)), (0xD0, ()), (0xD0, (2,)), (0xEA, (3,)), (0xEA, (0, 0)), (0x90, (1, 1))):
            with self.assertRaises(aerox.Refused):
                aerox.build_frame(cmd, args)

    def test_write_arguments_are_checked(self):
        record = [0x51, 4, 0, 0, 0]
        bad = (
            (0x6B, (4,)),                            # no such rate
            (0x6D, (0, 0, 4, 9, 13, 27, 38)),        # no stages
            (0x6D, (6, 0, 4, 9, 13, 27, 38)),        # too many
            (0x6D, (2, 2, 4, 9, 0, 0, 0)),           # current past the end
            (0x6D, (2, 0, 1, 9, 0, 0, 0)),           # 1 isn't a sensor value in the table
            (0x6E, (5,)),
            (0x69, (1, 0, 0, 0)),                    # not a half minute step
            (0x69, (0xFF, 0xFF, 0xFF, 0xFF)),        # huge
            (0x63, (16, 1, 1, 0, 0, 0, 0, 0)),       # brightness
            (0x63, (15, 16, 1, 0, 0, 0, 0, 0)),
            (0x66, (2, 0, 0, 0, 0)),
            (0x68, (2,)),
            (0x61, (4, *([0, 1, 2, 3] * 4))),        # too many zones
            (0x61, (1, 3, 1, 2, 3)),                 # no zone 3
            (0x6A, (3, *(record * 6))),              # no such page
            (0x6A, (0, *(record * 5))),              # short
            (0x6A, (0, 0x99, *([0] * 29))),          # unknown action
            (aerox.SAVE, (0,)),                      # saves the link blocks too
            (aerox.SAVE, (3,)),
            (aerox.SAVE, (4,)),
        )
        for cmd, args in bad:
            with self.assertRaises(aerox.Refused, msg=f"{cmd:#04x} {args}"):
                aerox.build_frame(cmd, args)

    def test_frame_shape(self):
        frame = aerox.build_frame(0x90, (1,))
        self.assertEqual(len(frame), 65)
        self.assertEqual(frame[:3], b"\x00\x90\x01")
        self.assertEqual(frame[3:], bytes(62))


class Link(unittest.TestCase):
    def setUp(self):
        self.ours, self.device = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        self.addCleanup(self.ours.close)
        self.addCleanup(self.device.close)
        self.mouse = aerox.Aerox("/dev/fake", "dongle", aerox.FIRMWARE_BCD, fd=self.ours.fileno())

    def answer(self, *replies):
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

    def test_a_refusal_from_the_mouse_is_an_error(self):
        self.answer(reply(0x6B, 2))
        with self.assertRaisesRegex(aerox.AeroxError, "refused"):
            self.mouse.write(0x6B, 1)

    def test_mouse_on_other_firmware_stops(self):
        self.answer(reply(*b"1.2.5"))
        with self.assertRaisesRegex(aerox.AeroxError, "1.2.5"):
            self.mouse.check_mouse_firmware()


class Settings(unittest.TestCase):
    """Every setter against the simulated mouse: the right bytes, then a confirmed read-back."""

    def setUp(self):
        self.ours, self.device = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        self.addCleanup(self.ours.close)
        self.addCleanup(self.device.close)
        self.sim = SimulatedMouse(self.device)
        self.mouse = aerox.Aerox("/dev/fake", "dongle", aerox.FIRMWARE_BCD, fd=self.ours.fileno())

        def write(fd, data):
            self.device.send(self.sim.answer(data))
            return len(data)

        patcher = mock.patch("aerox.os.write", write)
        patcher.start()
        self.addCleanup(patcher.stop)
        sleeper = mock.patch("aerox.time.sleep")
        sleeper.start()
        self.addCleanup(sleeper.stop)

    def sent(self, cmd):
        return [f[2:] for f in self.sim.frames if f[1] == cmd]

    def test_polling_waits_out_the_resync(self):
        self.mouse.set_polling(500)
        self.assertEqual(self.sim.polling, 1)
        self.assertEqual(self.sim.resync_reads, 0)

    def test_polling_already_right_sends_nothing(self):
        self.mouse.set_polling(1000)
        self.assertEqual(self.sent(0x6B), [])

    def test_polling_rejects_other_rates(self):
        with self.assertRaises(aerox.AeroxError):
            self.mouse.set_polling(750)

    def test_dpi(self):
        self.mouse.set_dpi([400, 1600], 1)
        self.assertEqual((self.sim.count, self.sim.current, self.sim.levels), (2, 1, [4, 18]))
        self.assertEqual(self.sent(0x6D)[0][:7], bytes([2, 1, 4, 18, 0, 0, 0]))

    def test_dpi_needs_real_values(self):
        for dpis in ([450], [18100], [50], []):
            with self.assertRaises(aerox.AeroxError):
                self.mouse.set_dpi(dpis)
        self.assertEqual(self.sent(0x6D), [])

    def test_stage_range(self):
        self.mouse.set_stage(4)
        self.assertEqual(self.sim.current, 4)
        with self.assertRaises(aerox.AeroxError):
            self.mouse.set_stage(5)

    def test_sleep(self):
        self.mouse.set_sleep_ms(600000)
        self.assertEqual(self.sim.sleep, 600000)
        for bad in (1000, 1230000):
            with self.assertRaises(aerox.AeroxError):
                self.mouse.set_sleep_ms(bad)

    def test_lighting_keeps_what_isnt_named(self):
        self.mouse.set_lighting(brightness=4)
        self.assertEqual(self.mouse.lighting(), {
            "brightness": 4, "idle_brightness": 1, "smart": True, "idle_timeout_ms": 30000,
        })

    def test_reactive(self):
        self.mouse.set_reactive(color="ff5a00")
        self.assertEqual(self.mouse.reactive(), {"enabled": True, "random": False, "color": "ff5a00"})

    def test_zone_colors(self):
        self.mouse.set_zone_colors({0: "ff0000", 2: "0000ff"})
        self.assertEqual(self.sim.colors, [2, 0, 255, 0, 0, 2, 0, 0, 255])
        self.mouse.clear_zone_colors()
        self.assertIsNone(self.sim.colors)

    def test_bind_alt_tab_on_side_12(self):
        self.mouse.bind("side 12", "alt+tab")
        self.assertEqual(self.sim.records[45:50], bytes([0x51, 0xE2, 0x2B, 0, 0]))
        self.assertEqual(self.mouse.buttons()[9]["action"], "alt+tab")

    def test_bind_only_writes_the_page_that_changed(self):
        self.mouse.bind("side 12", "alt+tab")
        self.assertEqual([f[0] for f in self.sent(0x6A)], [1])

    def test_bind_wheel_record_on_the_last_page(self):
        self.mouse.bind("wheel down", "none")
        pages = self.sent(0x6A)
        self.assertEqual(pages[0][0], 2)
        self.assertEqual(self.sim.records[95], 0)

    def test_save_is_only_the_two_blocks(self):
        self.mouse.save()
        self.assertEqual(self.sim.saved, [1, 2])
        self.sim.saved.clear()
        self.mouse.save("lighting")
        self.assertEqual(self.sim.saved, [2])

    def test_save_refuses_a_flat_battery(self):
        self.sim.battery = 2
        with self.assertRaisesRegex(aerox.AeroxError, "flat"):
            self.mouse.save()
        self.assertEqual(self.sim.saved, [])

    def test_save_on_a_low_battery_is_fine_while_charging(self):
        self.sim.battery = 0x80 | 2
        self.mouse.save("config")
        self.assertEqual(self.sim.saved, [1])

    def test_nothing_else_saves(self):
        self.mouse.bind("side 12", "alt+tab")
        self.mouse.set_dpi([800], 0)
        self.mouse.set_polling(250)
        self.mouse.set_lighting(brightness=1)
        self.assertEqual(self.sim.saved, [])

    def test_backup_and_restore(self):
        saved = json.loads(json.dumps(self.mouse.backup()))
        self.mouse.set_dpi([800], 0)
        self.mouse.set_polling(125)
        self.mouse.set_sleep_ms(0)
        self.mouse.set_low_power(True)
        self.mouse.set_lighting(brightness=1)
        self.mouse.set_reactive(enabled=False)
        self.mouse.bind("side 12", "alt+tab")
        self.mouse.restore(saved)
        self.assertEqual(self.mouse.backup(), {**saved, "dpi": saved["dpi"]})
        self.assertEqual(self.sim.saved, [])

    def test_link_down_skips_mouse_reads(self):
        self.sim.link = False
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

    def test_dpi_real_reply(self):
        got = aerox.decode_dpi(reply(0xED, 5, 0, 0x04, 0x09, 0x0D, 0x1B, 0x26))
        self.assertEqual(got["dpi"], [400, 800, 1200, 2400, 3200])
        self.assertEqual(got["levels"], [4, 9, 13, 27, 38])

    def test_dpi_uses_count(self):
        self.assertEqual(aerox.decode_dpi(reply(0xED, 2, 1, 4, 9, 13, 27, 38))["dpi"], [400, 800])

    def test_a_value_outside_the_table_stays_raw(self):
        self.assertEqual(aerox.decode_dpi(reply(0xED, 1, 0, 1))["dpi"], [None])
        self.assertIn("0x01", aerox.format_dpi(aerox.decode_dpi(reply(0xED, 1, 0, 1))))

    def test_dpi_table(self):
        self.assertEqual(len(aerox.DPI_LEVELS), 180)
        self.assertEqual(aerox.dpi_to_level(100), 0)
        self.assertEqual(aerox.dpi_to_level(3200), 38)
        self.assertEqual(aerox.dpi_to_level(18000), 214)
        self.assertEqual(aerox.level_to_dpi(214), 18000)

    def test_real_lighting_reply(self):
        got = aerox.decode_lighting(bytes.fromhex("e30f010100307500".ljust(128, "0")))
        self.assertEqual(got, {"brightness": 15, "idle_brightness": 1, "smart": True, "idle_timeout_ms": 30000})

    def test_real_reactive_reply(self):
        got = aerox.decode_reactive(bytes.fromhex("e60100ffffff".ljust(128, "0")))
        self.assertEqual(got, {"enabled": True, "random": False, "color": "ffffff"})

    def test_default_buttons(self):
        buttons = aerox.decode_buttons(b"".join(aerox.DEFAULTS))
        self.assertEqual(len(buttons), 20)
        self.assertTrue(all(b["default"] for b in buttons))
        by_name = {b["name"]: b["action"] for b in buttons}
        self.assertEqual(by_name["left"], "mouse1")
        self.assertEqual(by_name["tilt left"], "tilt-left")
        self.assertEqual(by_name["dpi"], "dpi")
        self.assertEqual(by_name["side 1"], "1")
        self.assertEqual(by_name["side 10"], "0")
        self.assertEqual(by_name["side 11"], "-")
        self.assertEqual(by_name["side 12"], "=")
        self.assertEqual(by_name["wheel up"], "scroll-up")

    def test_his_real_bindings(self):
        records = bytearray(b"".join(aerox.DEFAULTS))
        records[25:30] = bytes.fromhex("0600000000")
        records[45:50] = bytes.fromhex("002e000000")
        records[65:70] = bytes.fromhex("302d000000")
        by_name = {b["name"]: b for b in aerox.decode_buttons(records)}
        self.assertEqual(by_name["dpi"]["action"], "mouse6")
        self.assertEqual(by_name["side 12"]["action"], "none")
        self.assertEqual(by_name["side 11"]["action"], "dpi")
        self.assertEqual(sum(not b["default"] for b in by_name.values()), 3)

    def test_actions_round_trip(self):
        for text in ("alt+tab", "ctrl+c", "ctrl+shift+-", "f13", "mouse4", "dpi", "scroll-up", "scroll-down",
                     "tilt-left", "tilt-right", "media:mute", "media:volume-up", "none", "enter", "up"):
            self.assertEqual(aerox.describe_action(aerox.parse_action(text)), text)

    def test_action_spellings(self):
        self.assertEqual(aerox.parse_action("Alt+Tab"), bytes([0x51, 0xE2, 0x2B, 0, 0]))
        self.assertEqual(aerox.parse_action("keys:alt+tab"), bytes([0x51, 0xE2, 0x2B, 0, 0]))
        self.assertEqual(aerox.parse_action("control+escape"), bytes([0x51, 0xE0, 0x29, 0, 0]))
        self.assertEqual(aerox.parse_action("nothing"), bytes(5))

    def test_bad_actions(self):
        for text in ("banana", "a+b+c+d+e", "media:party", "mouse9", ""):
            with self.assertRaises(aerox.AeroxError):
                aerox.parse_action(text)

    def test_button_names(self):
        self.assertEqual(aerox.record_index("side 12"), 9)
        self.assertEqual(aerox.record_index("side-12"), 9)
        self.assertEqual(aerox.record_index("S12"), 9)
        self.assertEqual(aerox.record_index("Tilt Left"), 3)
        with self.assertRaises(aerox.AeroxError):
            aerox.record_index("side 13")

    def test_colors(self):
        self.assertEqual(aerox.parse_color("#FF5a00"), b"\xff\x5a\x00")
        with self.assertRaises(aerox.AeroxError):
            aerox.parse_color("red")

    def test_formats(self):
        self.assertEqual(aerox.format_timer(0), "off")
        self.assertEqual(aerox.format_timer(30000), "30 s")
        self.assertEqual(aerox.format_timer(300000), "5 min")


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

    def run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        ours, device = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        self.addCleanup(ours.close)
        self.addCleanup(device.close)
        sim = getattr(self, "sim", None) or SimulatedMouse(device)
        self.sim = sim
        sim.device = device

        def write(fd, data):
            device.send(sim.answer(data))
            return len(data)

        real = aerox.Aerox("/dev/fake", "dongle", aerox.FIRMWARE_BCD, fd=ours.fileno())
        real.close = lambda: None
        with mock.patch("aerox.Aerox.open", return_value=real), mock.patch("aerox.os.write", write), \
                mock.patch("aerox.time.sleep"), contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = aerox.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def test_status_is_the_default(self):
        code, out, _ = self.run_cli()
        self.assertEqual(code, 0)
        self.assertIn("[400] 800 1200 2400", out)

    def test_json_before_or_after_the_command(self):
        self.assertEqual(json.loads(self.run_cli("--json", "battery")[1])["level"], 5)
        self.assertEqual(json.loads(self.run_cli("battery", "--json")[1])["level"], 5)

    def test_bind_reports_it_isnt_saved(self):
        code, out, err = self.run_cli("bind", "side 12", "alt+tab")
        self.assertEqual(code, 0)
        self.assertIn("alt+tab", out)
        self.assertIn("aerox save", err)
        self.assertEqual(self.sim.saved, [])

    def test_save_asks_first(self):
        with mock.patch("builtins.input", return_value="n"):
            code, out, _ = self.run_cli("save")
        self.assertEqual(code, 1)
        self.assertEqual(self.sim.saved, [])

    def test_save_yes(self):
        code, _, _ = self.run_cli("save", "config", "--yes")
        self.assertEqual(code, 0)
        self.assertEqual(self.sim.saved, [1])

    def test_mouse_asleep_is_a_message(self):
        self.run_cli("battery")
        self.sim.link = False
        code, _, err = self.run_cli("battery")
        self.assertEqual(code, 1)
        self.assertIn("asleep", err)

    def test_status_while_asleep_still_works(self):
        self.run_cli("battery")
        self.sim.link = False
        code, out, _ = self.run_cli()
        self.assertEqual(code, 0)
        self.assertIn("not connected", out)

    def test_every_setting_from_the_command_line(self):
        for argv in (("dpi", "400,800,1600", "--stage", "2"), ("dpi", "--stage", "1"), ("polling", "500"), ("sleep", "5"),
                     ("low-power", "on"), ("light", "--brightness", "4", "--idle-minutes", "1", "--smart", "off"),
                     ("color", "all", "ff0000"), ("color", "clear"), ("reactive", "off", "--color", "00ff00")):
            code, _, err = self.run_cli(*argv)
            self.assertEqual(code, 0, f"{argv}: {err}")
        self.assertEqual(self.sim.saved, [])

    def test_backup_file_round_trip(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "mouse.json")
            self.assertEqual(self.run_cli("backup", path)[0], 0)
            self.run_cli("dpi", "800")
            self.assertEqual(self.run_cli("restore", path)[0], 0)
            self.assertEqual(self.sim.levels, [4, 9, 13, 27, 38])


if __name__ == "__main__":
    unittest.main()
