#!/usr/bin/env python3
"""Round-trip every setting on a real Aerox 9: change it, read it back, put it back.

Needs the mouse awake, and AEROX_HARDWARE=1. Only touches RAM: it never saves, so the worst
case is a setting left changed until the mouse powers off.

    AEROX_HARDWARE=1 python3 test_hardware.py
"""
import os
import time
import unittest

import aerox

@unittest.skipUnless(os.environ.get("AEROX_HARDWARE"), "set AEROX_HARDWARE=1 with the mouse connected")
class RealMouse(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mouse = aerox.Aerox.open()
        cls.mouse.ready()

    @classmethod
    def tearDownClass(cls):
        cls.mouse.close()

    def still_connected(self):
        for _ in range(20):
            try:
                if self.mouse.link_up():
                    return
            except aerox.MouseUnreachable:
                pass
            time.sleep(0.25)
        self.fail("the mouse never came back")

    def test_polling(self):
        before = self.mouse.polling()["hz"]
        self.addCleanup(self.mouse.set_polling, before)
        for hz in (500, 250, 125, 1000):
            self.mouse.set_polling(hz)
            self.still_connected()
            self.assertEqual(self.mouse.polling()["hz"], hz)

    def test_dpi(self):
        before = self.mouse.dpi()
        self.addCleanup(self.mouse.set_dpi, before["dpi"], before["current"])
        self.mouse.set_dpi([500, 1000, 1500], 1)
        got = self.mouse.dpi()
        self.assertEqual((got["dpi"], got["current"], got["count"]), ([500, 1000, 1500], 1, 3))

    def test_stage(self):
        before = self.mouse.dpi()
        self.addCleanup(self.mouse.set_stage, before["current"])
        other = (before["current"] + 1) % before["count"]
        self.mouse.set_stage(other)
        self.assertEqual(self.mouse.dpi()["current"], other)

    def test_low_power(self):
        before = self.mouse.low_power()
        self.addCleanup(self.mouse.set_low_power, before)
        self.mouse.set_low_power(not before)
        self.assertEqual(self.mouse.low_power(), not before)

    def test_lighting(self):
        before = self.mouse.lighting()
        self.addCleanup(self.mouse.set_lighting, **before)
        self.mouse.set_lighting(brightness=8, idle_brightness=0, smart=not before["smart"], idle_timeout_ms=60000)
        self.assertEqual(self.mouse.lighting(), {
            "brightness": 8, "idle_brightness": 0, "smart": not before["smart"], "idle_timeout_ms": 60000,
        })

    def test_only_the_named_lighting_value_changes(self):
        before = self.mouse.lighting()
        self.addCleanup(self.mouse.set_lighting, **before)
        self.mouse.set_lighting(brightness=4)
        self.assertEqual(self.mouse.lighting(), {**before, "brightness": 4})

    def test_reactive(self):
        before = self.mouse.reactive()
        self.addCleanup(self.mouse.set_reactive, **before)
        self.mouse.set_reactive(enabled=not before["enabled"], random=not before["random"], color="123456")
        self.assertEqual(self.mouse.reactive(), {
            "enabled": not before["enabled"], "random": not before["random"], "color": "123456",
        })

    def test_zone_colors(self):
        self.addCleanup(self.mouse.clear_zone_colors)
        self.mouse.set_zone_colors({0: "ff0000", 1: "00ff00", 2: "0000ff"})

    def test_buttons_every_page(self):
        before = self.mouse._button_records()
        self.addCleanup(self.mouse.set_records, before)
        for name, action, shown in (("tilt left", "mouse4", "mouse4"), ("side 9", "ctrl+c", "ctrl+c"),
                                    ("side 1", "media:mute", "media:mute"), ("wheel down", "none", "none")):
            self.mouse.bind(name, action)
            self.assertEqual(self.mouse.buttons()[aerox.record_index(name)]["action"], shown)

    def test_a_bind_leaves_other_buttons_alone(self):
        before = self.mouse.buttons()
        self.addCleanup(self.mouse.set_records, self.mouse._button_records())
        self.mouse.bind("side 2", "alt+f4")
        after = self.mouse.buttons()
        self.assertEqual([b for b in after if b["name"] != "side 2"], [b for b in before if b["name"] != "side 2"])

    def test_dangerous_commands_are_refused(self):
        for cmd in (0x91, 0xD1, 0x01, 0x41, 0x3F, 0x7F, 0x24, 0x64):
            with self.assertRaises(aerox.Refused):
                self.mouse.request(cmd, 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
