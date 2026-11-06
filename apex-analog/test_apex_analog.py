#!/usr/bin/env python3
"""python3 -m unittest    No keyboard needed. Virtual device tests hold their own output so nothing reaches your desktop."""
import fcntl
import os
import random
import select
import shutil
import socket
import struct
import subprocess
import tempfile
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
        self.assertEqual(set(a.SETTING_COMMANDS), {0x31})


if __name__ == "__main__":
    unittest.main()
