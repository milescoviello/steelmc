#!/usr/bin/env python3
"""python3 -m unittest    No keyboard needed. Virtual device tests hold their own output so nothing reaches your desktop."""
import fcntl
import os
import select
import struct
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
        self.assertEqual(set(a.ALLOWED), {0xD7, 0xDA, 0xB1, 0xB2, 0x9E})

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


class Firmware(unittest.TestCase):
    def setUp(self):
        original = a._find_control_node
        self.addCleanup(setattr, a, "_find_control_node", original)

    def test_refuses_any_other_firmware(self):
        a._find_control_node = lambda: ("/dev/null", "0417")
        with self.assertRaisesRegex(a.ApexError, "Refusing"):
            a.Keyboard()

    def test_missing_keyboard_is_explained(self):
        a._find_control_node = lambda: (None, None)
        with self.assertRaisesRegex(a.NotFound, "plugged in|bootloader"):
            a.Keyboard()


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


if __name__ == "__main__":
    unittest.main()
