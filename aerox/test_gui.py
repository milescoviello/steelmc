#!/usr/bin/env python3
"""GUI tests. Nothing goes on screen, and no device is touched: fakes stand in for both."""
import importlib.util
import os
import stat
import tempfile
import time
import unittest

os.environ["QT_QPA_PLATFORM"] = "offscreen"  # never on screen, whatever the session is

from PySide6.QtCore import QEventLoop, QPoint, Qt, QTimer
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

import aerox
import gui
import screenshots

app = QApplication.instance() or QApplication([])
gui.style_app(app)


class Inline:
    def submit(self, call, done=None, failed=None):
        try:
            value = call()
        except (aerox.AeroxError, gui.KeyboardError, OSError) as e:
            if failed:
                failed(e)
        else:
            if done:
                done(value)


class FakeMouse:
    def __init__(self):
        self.calls = []
        self.data = screenshots.sample_mouse()
        self.data["backup"] = {"marker": "taken at load"}
        self.fail = None

    def read(self):
        if self.fail:
            raise self.fail
        return self.data

    def battery(self):
        if self.fail:
            raise self.fail
        return self.data["battery"]

    def call(self, method, *args, **kwargs):
        if self.fail:
            raise self.fail
        self.calls.append((method, args, kwargs))
        return self.data["buttons"] if method == "buttons" else None

    def undo(self, backup):
        self.calls.append(("undo", (backup,), {}))


class FakeKeyboard:
    def __init__(self):
        self.calls = []
        self.data = screenshots.sample_keyboard()
        self.fail = None

    def read(self):
        if self.fail:
            raise self.fail
        return self.data

    def __getattr__(self, name):
        def record(*args):
            self.calls.append((name, *args))
        return record


def yes(*_):
    return True


def no(*_):
    return False


class MousePageTest(unittest.TestCase):
    def setUp(self):
        self.device = FakeMouse()
        self.page = gui.MousePage(self.device, Inline(), yes)
        self.page.refresh()

    def wait_timers(self):
        for timer in (self.page.dpi_timer, self.page.light_timer):
            if timer.isActive():
                timer.stop()
                timer.timeout.emit()

    def test_loading_sends_nothing(self):
        self.wait_timers()
        self.assertEqual(self.device.calls, [])
        self.assertFalse(self.page.save_button.isEnabled())

    def test_shows_what_the_mouse_says(self):
        self.assertEqual([s.value() for _, s in self.page.stage_rows], [400, 800, 1200, 2400, 3200])
        self.assertTrue(self.page.stage_rows[0][0].isChecked())
        self.assertEqual(self.page.polling.currentData(), 1000)
        self.assertEqual(self.page.sleep.currentData(), 300000)
        self.assertEqual(self.page.brightness.value(), 15)
        self.assertEqual(self.page.reactive_color.color, "ffffff")
        self.assertEqual(self.page.caps["side 12"].detail, "Alt+Tab")
        self.assertEqual(self.page.caps["side 1"].detail, "Key 1")
        self.assertEqual(self.page.caps["dpi"].detail, "Button 6")
        self.assertEqual(self.page.battery_label.text(), "20%")

    def test_a_timer_value_outside_the_menu_still_shows(self):
        self.device.data["sleep_ms"] = 1800000
        self.page.refresh()
        self.assertEqual(self.page.sleep.currentData(), 1800000)
        self.assertEqual(self.device.calls, [])

    def test_polling(self):
        self.page.polling.setCurrentIndex(self.page.polling.findData(500))
        self.assertEqual(self.device.calls, [("set_polling", (500,), {})])
        self.assertTrue(self.page.save_button.isEnabled())

    def test_dpi_edit_waits_then_sends_every_stage(self):
        self.page.stage_rows[1][1].setValue(900)
        self.page.stage_rows[2][1].setValue(1300)
        self.assertEqual(self.device.calls, [])
        self.wait_timers()
        self.assertEqual(self.device.calls, [("set_dpi", ([400, 900, 1300, 2400, 3200], 0), {})])

    def test_dpi_rounds_to_what_the_sensor_takes(self):
        self.page.stage_rows[0][1].setValue(450)
        self.wait_timers()
        self.assertEqual(self.device.calls[0][1][0][0], 400)

    def test_stage_pick(self):
        self.page.stage_rows[2][0].click()
        self.assertEqual(self.device.calls, [("set_stage", (2,), {})])

    def test_fewer_stages(self):
        self.page.stage_rows[4][0].click()
        self.device.calls.clear()
        self.page.stage_count.setCurrentIndex(1)
        self.assertFalse(self.page.stage_rows[3][1].isEnabled())
        self.wait_timers()
        self.assertEqual(self.device.calls, [("set_dpi", ([400, 800], 0), {})])

    def test_sleep_and_low_power(self):
        self.page.sleep.setCurrentIndex(self.page.sleep.findData(600000))
        self.page.low_power.setChecked(True)
        self.assertEqual(self.device.calls, [("set_sleep_ms", (600000,), {}), ("set_low_power", (True,), {})])

    def test_lighting_sends_one_change_for_a_slider_drag(self):
        for value in (10, 9, 8):
            self.page.brightness.setValue(value)
        self.wait_timers()
        self.assertEqual(self.device.calls, [("set_lighting", (), {
            "brightness": 8, "idle_brightness": 1, "smart": True, "idle_timeout_ms": 30000,
        })])

    def test_reactive(self):
        self.page.reactive.setChecked(False)
        self.assertEqual(self.device.calls[-1], ("set_reactive", (), {"enabled": False, "random": False, "color": "ffffff"}))

    def test_zone_colors_cant_be_saved_so_they_dont_ask_to(self):
        self.page.zone_picked(1, "ff0000")
        self.assertEqual(self.device.calls, [("set_zone_colors", ({1: "ff0000"},), {})])
        self.assertFalse(self.page.save_button.isEnabled())

    def test_bind_alt_tab(self):
        self.page.caps["side 2"].click()
        self.assertEqual(self.page.bind_button.text(), "Bind to Side 2")
        self.page.keys_edit.setCurrentText("alt+tab")
        self.page.bind_button.click()
        self.assertEqual(self.device.calls[0], ("bind", ("side 2", aerox.parse_action("alt+tab")), {}))
        self.assertEqual(self.device.calls[1][0], "buttons")

    def test_picking_a_button_shows_its_action(self):
        self.page.caps["side 12"].click()
        self.assertEqual((self.page.kind.currentData(), self.page.keys_edit.currentText()), ("keys", "alt+tab"))
        self.page.caps["dpi"].click()
        self.assertEqual((self.page.kind.currentData(), self.page.mouse_number.currentData()), ("mouse", "mouse6"))
        self.page.caps["side 11"].click()
        self.assertEqual(self.page.kind.currentData(), "dpi")
        self.assertEqual(self.device.calls, [])

    def test_bad_keys_are_a_message_not_a_write(self):
        self.page.caps["side 2"].click()
        self.page.keys_edit.setCurrentText("banana")
        self.page.bind_button.click()
        self.assertEqual(self.device.calls, [])
        self.assertIn("banana", self.page.status.text())

    def test_other_kinds_of_action(self):
        self.page.caps["side 3"].click()
        for kind, want in (("dpi", "dpi"), ("scroll-up", "scroll-up"), ("none", "none")):
            self.page.kind.setCurrentIndex(self.page.kind.findData(kind))
            self.assertEqual(self.page.action_text(), want)
        self.page.kind.setCurrentIndex(self.page.kind.findData("media"))
        self.page.media_key.setCurrentIndex(self.page.media_key.findData("media:mute"))
        self.assertEqual(self.page.action_text(), "media:mute")

    def test_reset_uses_the_firmwares_default(self):
        self.page.caps["side 12"].click()
        self.page.default_button.click()
        self.assertEqual(self.device.calls[0][1], ("side 12", aerox.DEFAULTS[9]))

    def test_save_asks_first(self):
        self.page.confirm = no
        self.page.low_power.setChecked(True)
        self.device.calls.clear()
        self.page.save_button.click()
        self.assertEqual(self.device.calls, [])

    def test_save(self):
        self.page.low_power.setChecked(True)
        self.device.calls.clear()
        self.page.save_button.click()
        self.assertEqual(self.device.calls, [("save", (), {})])
        self.assertFalse(self.page.save_button.isEnabled())
        self.assertIn("Saved", self.page.status.text())

    def test_nothing_saves_on_its_own(self):
        self.page.low_power.setChecked(True)
        self.page.caps["side 2"].click()
        self.page.bind_button.click()
        self.page.polling.setCurrentIndex(1)
        self.assertNotIn("save", [c[0] for c in self.device.calls])

    def test_undo_goes_back_to_what_was_loaded(self):
        self.page.low_power.setChecked(True)
        self.page.undo_button.click()
        self.assertEqual(self.device.calls[-1], ("undo", ({"marker": "taken at load"},), {}))
        self.assertFalse(self.page.undo_button.isEnabled())

    def test_a_reload_while_changed_keeps_the_original_to_undo_to(self):
        self.page.low_power.setChecked(True)
        self.device.data["backup"] = {"marker": "later"}
        self.page.refresh()
        self.assertEqual(self.page.backup, {"marker": "taken at load"})
        self.assertTrue(self.page.save_button.isEnabled())

    def test_asleep(self):
        self.device.fail = aerox.MouseUnreachable("It's asleep: move it and retry.")
        self.page.refresh()
        self.assertFalse(self.page.content.isEnabled())
        self.assertFalse(self.page.retry.isHidden())
        self.assertFalse(self.page.save_button.isEnabled())
        self.assertIn("asleep", self.page.status.text())
        self.device.fail = None
        self.page.retry.click()
        self.assertTrue(self.page.content.isEnabled())
        self.assertTrue(self.page.retry.isHidden())
        self.assertNotIn("asleep", self.page.status.text())

    def test_polling_notices_it_falling_asleep_and_waking(self):
        self.device.fail = aerox.MouseUnreachable("asleep")
        self.page.poll()
        self.assertFalse(self.page.connected)
        self.device.fail = None
        self.page.poll()
        self.assertTrue(self.page.connected)

    def test_asleep_doesnt_show_a_stale_battery(self):
        self.assertEqual(self.page.meter.level, 5)
        self.device.fail = aerox.MouseUnreachable("asleep")
        self.page.refresh()
        self.assertEqual(self.page.meter.level, 0)
        self.assertEqual(self.page.battery_label.text(), "")

    def test_a_write_that_loses_the_mouse(self):
        self.device.fail = aerox.MouseUnreachable("asleep")
        self.page.polling.setCurrentIndex(1)
        self.assertFalse(self.page.connected)

    def test_a_refused_write_leaves_the_page_usable(self):
        self.device.fail = aerox.AeroxError("The mouse refused it.")
        self.page.polling.setCurrentIndex(1)
        self.assertTrue(self.page.connected)
        self.assertIn("refused", self.page.status.text())
        self.assertFalse(self.page.save_button.isEnabled())


class KeyboardPageTest(unittest.TestCase):
    def setUp(self):
        self.device = FakeKeyboard()
        self.page = gui.KeyboardPage(self.device, Inline(), yes)
        self.page.refresh()

    def test_loading_sends_nothing(self):
        self.assertEqual(self.device.calls, [])

    def test_caps_show_depth(self):
        self.assertEqual(self.page.caps["W"].detail, "1.0 R")
        self.assertAlmostEqual(self.page.caps["W"].fill, 0.25)
        self.assertEqual(self.page.caps["J"].detail, "1.8")
        self.assertEqual(self.page.caps["SPACE"].detail, "3.0")

    def test_keys_without_sensors_cant_be_picked(self):
        cap = self.page.caps["ESC"]
        self.assertTrue(cap.inert)
        QTest.mouseClick(cap, Qt.LeftButton)
        self.assertEqual(self.page.selected_keys(), [])
        self.page.pick(None)
        self.assertNotIn("ESC", self.page.selected_keys())
        self.assertNotIn("UP", self.page.selected_keys())
        self.assertEqual(len(self.page.selected_keys()), 61)

    def test_every_sensor_key_is_on_the_plate(self):
        self.assertEqual(set(self.device.data["info"]["keys"]) - set(self.page.caps), set())

    def test_set_actuation(self):
        self.page.pick(["W", "A"])
        self.page.depth.setValue(gui.DEPTHS.index(1.4))
        self.assertEqual(self.page.set_depth.text(), "Set 2 keys to 1.4 mm")
        self.page.set_depth.click()
        self.assertEqual(self.device.calls[0][0], "set_actuation")
        self.assertEqual(set(self.device.calls[0][1]), {"W", "A"})
        self.assertEqual(self.device.calls[0][2], 1.4)

    def test_nothing_picked_nothing_to_do(self):
        self.assertFalse(self.page.set_depth.isEnabled())
        self.assertFalse(self.page.rapid_on.isEnabled())

    def test_one_key_shows_its_own_depth(self):
        self.page.pick(["SPACE"])
        self.assertEqual(gui.DEPTHS[self.page.depth.value()], 3.0)

    def test_rapid(self):
        self.page.pick(["W"])
        self.page.rapid_off.click()
        self.assertEqual(self.device.calls, [("set_rapid", ["W"], False)])

    def test_lighting_shows_whats_running(self):
        self.assertEqual(self.page.effect.currentData(), ["lights", "--keep"])
        self.assertEqual(self.page.effect_color.color, "ffffff")

    def test_lighting_with_a_custom_color_shows_it(self):
        self.device.data["jobs"][0]["command"] = "lights --keep --color FF0000"
        self.page.refresh()
        self.assertEqual(self.page.effect.currentData(), ["lights", "--keep"])
        self.assertEqual(self.page.effect_color.color, "ff0000")

    def test_no_lights_job_means_normal_lighting(self):
        self.device.data["jobs"] = []
        self.page.refresh()
        self.assertIsNone(self.page.effect.currentData())
        self.assertFalse(self.page.effect_color.isEnabled())

    def test_effects_without_a_color_dont_send_one(self):
        self.page.effect.setCurrentIndex(3)
        self.assertFalse(self.page.effect_color.isEnabled())
        self.page.apply_effect()
        self.assertEqual(self.device.calls, [("lights", ["lights", "--effect", "ripple"], None)])

    def test_normal_lighting(self):
        self.page.effect.setCurrentIndex(0)
        self.page.apply_effect()
        self.assertEqual(self.device.calls, [("lights", None, None)])

    def test_features(self):
        self.page.feature_boxes["oled"].click()
        self.assertEqual(self.device.calls, [("start", "oled")])

    def test_feature_state_comes_from_the_daemon(self):
        self.device.data["jobs"].append({"name": "gamepad", "on": True, "command": "gamepad", "age": "1 min"})
        self.page.refresh()
        self.assertTrue(self.page.feature_boxes["gamepad"].isChecked())
        self.page.feature_boxes["gamepad"].click()
        self.assertEqual(self.device.calls, [("stop", "gamepad")])

    def test_turn_off_buttons_follow_the_jobs(self):
        self.assertEqual(self.page.turn_off.count(), 2)
        self.device.data["jobs"] = []
        self.page.refresh()
        self.assertEqual(self.page.turn_off.count(), 0)
        self.assertIn("Nothing", self.page.jobs_label.text())

    def test_presets(self):
        self.page.preset.setCurrentText("beamng")
        self.page.use_preset()
        self.assertEqual(self.device.calls, [("use_preset", "beamng")])

    def test_delete_asks_first(self):
        self.page.confirm = no
        self.page.delete_preset()
        self.assertEqual(self.device.calls, [])

    def test_save_asks_first(self):
        self.page.confirm = no
        self.page.save_button.click()
        self.assertEqual(self.device.calls, [])
        self.page.confirm = yes
        self.page.save_button.click()
        self.assertEqual(self.device.calls, [("save",)])

    def test_reports(self):
        self.device.report = lambda name: f"{name} says fine"
        self.page.show_report("health")
        self.assertEqual(self.page.report.text(), "health says fine")

    def test_keyboard_gone(self):
        self.device.fail = gui.KeyboardError("Keyboard not connected.")
        self.page.refresh()
        self.assertFalse(self.page.content.isEnabled())
        self.assertFalse(self.page.save_button.isEnabled())
        self.assertIn("not connected", self.page.status.text())
        self.device.fail = None
        self.page.retry.click()
        self.assertTrue(self.page.content.isEnabled())


class Helpers(unittest.TestCase):
    def test_parse_jobs_real_output(self):
        status = ("Daemon running, process 7774. Keyboard: connected, firmware 0416.\n"
                  "lights     on      lights --keep  (135 min)\n"
                  "recording  on      record  (135 min)\n")
        self.assertEqual(gui.parse_jobs(status), [
            {"name": "lights", "on": True, "command": "lights --keep", "age": "135 min"},
            {"name": "recording", "on": True, "command": "record", "age": "135 min"},
        ])

    def test_pretty_actions(self):
        for action, shown in (("alt+tab", "Alt+Tab"), ("ctrl+shift+z", "Ctrl+Shift+Z"), ("mouse6", "Button 6"), ("dpi", "Next DPI"),
                              ("none", "Nothing"), ("media:mute", "Mute"), ("scroll-up", "Scroll up"), ("tilt-left", "Tilt left"),
                              ("1", "Key 1"), ("f13", "Key F13"), ("enter", "Key Enter")):
            self.assertEqual(gui.pretty_action(action), shown)

    def test_every_side_button_is_on_the_grid(self):
        names = [n for row in gui.SIDE_GRID for n in row] + gui.TOP_BUTTONS
        self.assertEqual(sorted(names), sorted(aerox.RECORD_NAMES))

    def test_timers_match_ggs_menu(self):
        self.assertEqual(gui.TIMERS[0], 0)
        self.assertEqual(gui.TIMERS[-1], 1200000)
        self.assertEqual(len(gui.TIMERS), 41)

    def test_key_args_are_comma_safe(self):
        self.assertEqual(gui.KeyboardDevice.key_args(["W", ",", "-", "SPACE"]), "W,COMMA,MINUS,SPACE")

    @unittest.skipUnless(os.path.exists(os.path.expanduser("~/apex-analog/apex_analog.py")), "apex-analog isn't here")
    def test_apex_analog_understands_every_key_we_send(self):
        spec = importlib.util.spec_from_file_location("apex_analog", os.path.expanduser("~/apex-analog/apex_analog.py"))
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        names = [n for row in gui.PLATE for n, _ in row if n and n in module.BY_NAME]
        self.assertEqual(len(names), 61)
        self.assertEqual(module.key_names(gui.KeyboardDevice.key_args(names)), names)
        for name in names:
            self.assertEqual(module.key_names(gui.KeyboardDevice.key_args([name])), [name])


class KeyboardCommand(unittest.TestCase):
    def script(self, body):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        path = os.path.join(d.name, "fake-apex")
        with open(path, "w") as f:
            f.write("#!/bin/sh\n" + body)
        os.chmod(path, os.stat(path).st_mode | stat.S_IEXEC)
        return gui.KeyboardDevice(path)

    def test_arguments_arrive_as_they_are(self):
        device = self.script('printf "%s|" "$@"\n')
        self.assertEqual(device.run("actuation", "W,A", "1"), "actuation|W,A|1|")

    def test_error_message_comes_through(self):
        device = self.script('echo "apex-analog: keyboard not found" >&2\nexit 1\n')
        with self.assertRaisesRegex(gui.KeyboardError, "keyboard not found"):
            device.run("status")

    def test_not_installed(self):
        with self.assertRaisesRegex(gui.KeyboardError, "isn't installed"):
            gui.KeyboardDevice("/nonexistent/apex-analog").run("status")

    def test_a_hung_command_doesnt_hang_the_app(self):
        device = self.script("sleep 5\n")
        with self.assertRaisesRegex(gui.KeyboardError, "didn't finish"):
            device.run("status", timeout=0.1)

    def test_commands(self):
        device = self.script('printf "%s " "$@"\n')
        self.assertEqual(device.set_actuation(["W", ","], 1.4), "actuation W,COMMA 1.4 ")
        self.assertEqual(device.set_rapid(["W"], False), "rapid W off ")
        self.assertEqual(device.lights(None), "lights --off ")
        self.assertEqual(device.lights(["lights", "--keep"], "ffffff"), "lights --keep --color ffffff ")
        self.assertEqual(device.save(), "save --yes ")

    def test_report_is_just_the_summary(self):
        device = self.script('printf "6909 presses.\\nTry 1.4 mm.\\n\\nkey  presses\\nSPACE 1406\\n"\n')
        self.assertEqual(device.report("profile"), "6909 presses.\nTry 1.4 mm.")

    def test_real_shape_read(self):
        device = self.script('case "$1" in\ninfo) echo \'{"firmware":"0416","keys":{}}\';;\n'
                             'status) echo "Daemon running.";;\npreset) echo one; echo two;;\nesac\n')
        data = device.read()
        self.assertEqual(data["presets"], ["one", "two"])
        self.assertEqual(data["jobs"], [])


class RunnerThread(unittest.TestCase):
    def test_results_come_back_on_the_gui_thread(self):
        runner = gui.Runner()
        loop = QEventLoop()
        seen = {}

        def done(value):
            seen["value"] = value
            seen["gui_thread"] = app.thread().isCurrentThread()
            loop.quit()

        runner.submit(lambda: 41 + 1, done, self.fail)
        QTimer.singleShot(2000, loop.quit)
        loop.exec()
        self.assertEqual(seen, {"value": 42, "gui_thread": True})

    def test_errors_come_back_too(self):
        runner = gui.Runner()
        loop = QEventLoop()
        seen = []

        def boom():
            raise aerox.AeroxError("nope")

        runner.submit(boom, self.fail, lambda e: (seen.append(str(e)), loop.quit()))
        QTimer.singleShot(2000, loop.quit)
        loop.exec()
        self.assertEqual(seen, ["nope"])

    def test_a_slow_call_doesnt_block_the_window(self):
        runner = gui.Runner()
        loop = QEventLoop()
        start = time.monotonic()
        runner.submit(lambda: time.sleep(0.2), lambda _: loop.quit())
        self.assertLess(time.monotonic() - start, 0.1)
        QTimer.singleShot(2000, loop.quit)
        loop.exec()


class Window(unittest.TestCase):
    def test_fits_and_has_both_pages(self):
        window = gui.MainWindow(FakeMouse(), FakeKeyboard(), (Inline(), Inline()), yes)
        window.load()
        self.assertEqual(window.tabs.count(), 2)
        self.assertLessEqual(window.minimumSize().width(), 1100)
        self.assertLessEqual(window.minimumSize().height(), 800)
        window.close()

    def test_keycap_paints_in_every_state(self):
        cap = gui.KeyCap("W", "1.0")
        cap.resize(50, 50)
        cap.fill = 0.25
        cap.marked = True
        for checked in (False, True):
            cap.setChecked(checked)
            self.assertFalse(cap.grab().isNull())
        self.assertFalse(gui.KeyCap("Esc", inert=True).grab().isNull())


if __name__ == "__main__":
    unittest.main()
