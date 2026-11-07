#!/usr/bin/env python3
"""GUI tests. Nothing goes on screen, and no device is touched: fakes stand in for both."""
import base64
import importlib.util
import json
import os
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest

os.environ["QT_QPA_PLATFORM"] = "offscreen"  # never on screen, whatever the session is

from PySide6.QtCore import QEvent, QEventLoop, QPoint, QPointF, Qt, QTimer
from PySide6.QtGui import QMouseEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QPushButton

import gui
import aerox
import screenshots

app = QApplication.instance() or QApplication([])
gui.style_app(app)


class Inline:
    def submit(self, call, done=None, failed=None):
        try:
            value = call()
        except (aerox.AeroxError, gui.arctis.ArctisError, gui.gear.GearError, gui.KeyboardError, OSError) as e:
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

    def set_controller(self, config, run=True):
        self.calls.append(("set_controller", config, run))
        self.data = {**self.data, "controller": config}

    def __getattr__(self, name):
        if name == "stream_command":
            raise AttributeError(name)
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
        self.assertAlmostEqual(self.page.caps["W"].fill, 1.08 / 4)
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

    def test_experimental_model_says_so(self):
        self.device.data = {**self.device.data, "info": {**self.device.data["info"], "model": "Apex Pro TKL", "changes": "experimental"}}
        self.page.refresh()
        self.assertEqual(self.page.connection.text(), "Apex Pro TKL")
        self.assertIn("experimental", self.page.status.text())

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
        self.device.data["jobs"].append({"name": "oled", "on": True, "command": "oled", "age": "1 min"})
        self.page.refresh()
        self.assertTrue(self.page.feature_boxes["oled"].isChecked())
        self.page.feature_boxes["oled"].click()
        self.assertEqual(self.device.calls, [("stop", "oled")])

    def window(self):
        self.page.open_controller()
        self.addCleanup(self.page.controller_window.close)
        return self.page.controller_window

    def pick(self, w, *names):
        for name in names:
            QTest.mouseClick(w.caps[name], Qt.LeftButton)

    def role(self, w, out):
        return next(b for b in w.role_buttons if b.toolTip().startswith(f"The picked keys act as {gui.controller_label(out)}"))

    def test_the_controller_has_its_own_window(self):
        w = self.window()
        self.assertTrue(w.isVisible())
        self.assertTrue(w.caps["F1"].inert)  # no sensor, so no axis
        self.assertTrue(w.caps["SS"].inert)  # the SteelSeries key stays itself
        self.assertFalse(any(b.isEnabled() for b in w.role_buttons))
        self.pick(w, "W")
        self.role(w, "ly-").click()
        name, config, run = self.device.calls[-1]
        self.assertEqual((name, config["keys"], run), ("set_controller", {"W": "ly-"}, True))
        self.assertEqual(w.selected(), [])  # picks clear after each change
        self.assertEqual(w.caps["W"].detail, "LS\u2191")
        self.assertEqual(w.table.rowCount(), 1)
        w.table.cellWidget(0, 2).click()
        self.assertEqual(self.device.calls[-1][1]["keys"], {})

    def test_quick_setups_and_axes(self):
        w = self.window()
        quick = {b.text(): b for b in w.findChildren(QPushButton)}
        quick["W A S D as the left stick"].click()
        self.assertEqual(self.device.calls[-1][1]["keys"], {"W": "ly-", "S": "ly+", "A": "lx-", "D": "lx+"})
        quick["Every key its own axis"].click()
        keys = self.device.calls[-1][1]["keys"]
        self.assertGreater(len(keys), 40)
        self.assertEqual(sorted(gui.axis_of(v)[0] for v in keys.values()), list(range(1, len(keys) + 1)))
        quick["Clear all"].click()
        self.assertEqual(self.device.calls[-1][1]["keys"], {})
        self.pick(w, "J", "K")
        w.own_axis.click()
        self.assertEqual(set(self.device.calls[-1][1]["keys"].values()), {"axis:1", "axis:2"})

    def test_two_keys_share_one_axis(self):
        w = self.window()
        self.pick(w, "LSHIFT")
        self.assertFalse(w.shared_axis.isEnabled())  # it takes exactly two
        self.pick(w, "LCTRL")
        w.shared_axis.click()
        self.assertEqual(self.device.calls[-1][1]["keys"], {"LSHIFT": "axis:1+", "LCTRL": "axis:1-"})
        self.assertEqual(w.caps["LSHIFT"].detail, "Ax1\u2191")

    def test_settings_and_rules(self):
        w = self.window()
        w.toggle.setCurrentIndex(w.toggle.findData("G"))
        w.deadzone.setValue(0.5)
        w.curve.setValue(1.5)
        w.type_too.setChecked(True)
        w.rules.setPlainText("W=rt  # gas\nS=lt\nLSHIFT=axis:3+\nLCTRL=axis:3-")
        w.apply_rules()
        config = self.device.calls[-1][1]
        self.assertEqual(config["keys"], {"W": "rt", "S": "lt", "LSHIFT": "axis:3+", "LCTRL": "axis:3-"})
        self.assertEqual((config["toggle"], config["deadzone"], config["curve"], config["type_too"]), ("G", 0.5, 1.5, True))
        argv = gui.KeyboardDevice.controller_args(config)
        self.assertIn("LSHIFT=axis:3+", argv)
        self.assertIn("--no-swallow", argv)
        self.assertEqual(argv[argv.index("--toggle-colors") + 1], "00ff00,ff0000")
        calls = len(self.device.calls)
        for bad in ("W=ly-\nQQ=lt", "W=nonsense", "W=axis:2\nS=axis:2+"):
            w.rules.setPlainText(bad)
            w.apply_rules()
        self.assertEqual(len(self.device.calls), calls)
        w.toggle.setCurrentIndex(w.toggle.findData("W"))  # W is on the controller, so it can't be the toggle
        w.submit(dict(config["keys"]), "")
        self.assertEqual(len(self.device.calls), calls)

    def test_turn_off_buttons_follow_the_jobs(self):
        self.assertEqual(self.page.turn_off.count(), 2)
        self.device.data["jobs"] = []
        self.page.refresh()
        self.assertEqual(self.page.turn_off.count(), 0)
        self.assertIn("Nothing", self.page.jobs_label.text())

    def test_presets(self):
        self.page.preset.setCurrentText("gaming")
        self.page.use_preset()
        self.assertEqual(self.device.calls, [("use_preset", "gaming")])

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

    def test_keys_fill_as_you_press(self):
        cap = self.page.caps["W"]  # fires at 1.08 mm
        self.page.show_depths({"W": 0.5})
        self.assertEqual(cap.live_mm, 0.5)
        self.assertFalse(cap.fired())
        self.page.show_depths({"W": 3.2, "J": 0.4})
        self.assertTrue(cap.fired())
        self.assertEqual(self.page.caps["J"].live_mm, 0.4)
        self.assertFalse(cap.grab().isNull())

    def test_keys_empty_when_released(self):
        self.page.show_depths({"W": 3.2, "J": 0.4})
        self.page.show_depths({"J": 0.2})
        self.assertEqual(self.page.caps["W"].live_mm, 0.0)
        self.assertEqual(self.page.caps["J"].live_mm, 0.2)
        self.page.show_depths({})
        self.assertEqual(self.page.caps["J"].live_mm, 0.0)

    def test_a_key_without_a_sensor_or_a_name_we_dont_know_is_ignored(self):
        self.page.show_depths({"ESC": 1.0, "NOPE": 1.0})
        self.assertEqual(self.page.caps["ESC"].live_mm, 0.0)

    def test_pressing_sends_nothing(self):
        self.page.show_depths({"W": 3.2})
        self.assertEqual(self.device.calls, [])

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


class ShortcutPanel(unittest.TestCase):
    def setUp(self):
        self.device = FakeKeyboard()
        self.page = gui.KeyboardPage(self.device, Inline(), yes)
        self.page.refresh()

    def kind(self, value):
        self.page.shortcut_kind.setCurrentIndex(self.page.shortcut_kind.findData(value))

    def test_needs_exactly_one_key(self):
        self.assertFalse(self.page.shortcut_save.isEnabled())
        self.page.pick(["W", "A"])
        self.assertFalse(self.page.shortcut_save.isEnabled())
        self.page.pick(["J"])
        self.assertTrue(self.page.shortcut_save.isEnabled())
        self.assertEqual(self.page.shortcut_key.text(), "SteelSeries + J")

    def test_the_keyboards_own_keys_are_refused(self):
        for key in ("Q", "I", "O", "T", "LGUI"):
            self.page.pick([key])
            self.assertFalse(self.page.shortcut_save.isEnabled(), key)
            self.assertIn("keyboard's own", self.page.shortcut_key.text())

    def test_the_steelseries_key_itself_is_a_tap(self):
        self.page.pick(["SS"])
        self.assertTrue(self.page.shortcut_save.isEnabled())
        self.assertEqual(self.page.shortcut_key.text(), "Tap the SteelSeries key alone")
        self.kind("volume+")
        self.page.shortcut_save.click()
        self.assertEqual(self.device.calls[-1], ("add_shortcut", "SS", "volume+", ""))

    def test_save_keys(self):
        self.page.pick(["J"])
        self.kind("keys")
        self.page.shortcut_value.setText("ctrl+c")
        self.page.shortcut_save.click()
        self.assertEqual(self.device.calls, [("add_shortcut", "J", "keys", "ctrl+c")])

    def test_a_volume_shortcut_needs_nothing_typed(self):
        self.page.pick(["J"])
        self.kind("volume+")
        self.assertTrue(self.page.shortcut_value.isHidden())
        self.page.shortcut_save.click()
        self.assertEqual(self.device.calls, [("add_shortcut", "J", "volume+", "")])

    def test_empty_is_a_message_not_a_shortcut(self):
        self.page.pick(["J"])
        self.kind("command")
        self.page.shortcut_save.click()
        self.assertEqual(self.device.calls, [])
        self.assertIn("Say what", self.page.status.text())

    def test_existing_shortcuts_show_and_can_be_removed(self):
        self.device.data["shortcuts"] = {"J": "volume+", ",": "macro:ss-comma"}
        self.page.refresh()
        self.assertTrue(self.page.caps["J"].marked)
        self.assertIn("volume+", self.page.caps["J"].toolTip())
        self.assertIn("J: volume+", self.page.shortcut_list.text())
        self.page.pick(["K"])
        self.assertFalse(self.page.shortcut_remove.isEnabled())
        self.page.pick(["J"])
        self.page.shortcut_remove.click()
        self.assertEqual(self.device.calls, [("remove_shortcut", "J")])

    def test_commands_it_sends(self):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        path = os.path.join(d.name, "fake")
        with open(path, "w") as f:
            f.write('#!/bin/sh\nprintf "%s|" "$@" >> "$0.log"\nprintf "\\n" >> "$0.log"\n')
        os.chmod(path, 0o755)
        device = gui.KeyboardDevice(path)
        device.add_shortcut(",", "keys", "ctrl+c")
        device.add_shortcut("J", "text", "hi there")
        device.add_shortcut("K", "command", "firefox --new-window")
        device.add_shortcut("L", "volume+", "")
        device.remove_shortcut(",")
        self.assertEqual(open(path + ".log").read().splitlines(), [
            "macro|add|ss-comma|CTRL+C|", "shortcut|add|COMMA|macro:ss-comma|",
            "macro|add|ss-j|text:hi there|", "shortcut|add|J|macro:ss-j|",
            "shortcut|add|K|run:firefox --new-window|", "shortcut|add|L|volume+|", "shortcut|remove|COMMA|",
        ])


class DragSelect(unittest.TestCase):
    def setUp(self):
        self.device = FakeKeyboard()
        self.page = gui.KeyboardPage(self.device, Inline(), yes)
        self.page.refresh()
        self.plate = self.page.plate
        self.pressed_on = None

    def center(self, name):
        return self.page.caps[name].geometry().center()

    def event(self, widget, kind, plate_point, button=Qt.LeftButton):
        """Mouse events go to whichever widget took the press, in its own coordinates, as in real use."""
        local = QPointF(widget.mapFrom(self.plate, plate_point)) if widget is not self.plate else QPointF(plate_point)
        pressed = Qt.NoButton if kind == QEvent.MouseButtonRelease else Qt.LeftButton
        QApplication.sendEvent(widget, QMouseEvent(kind, local, local, button, pressed, Qt.NoModifier))

    def drag(self, *names):
        """Press on the first key, then move across the others, then let go."""
        start = self.page.caps[names[0]]
        self.event(start, QEvent.MouseButtonPress, self.center(names[0]))
        for name in names[1:]:
            self.event(start, QEvent.MouseMove, self.center(name), Qt.NoButton)
        self.event(start, QEvent.MouseButtonRelease, self.center(names[-1]))

    def picked(self):
        return set(self.page.selected_keys())

    def test_a_click_still_toggles_one_key(self):
        self.drag("J")
        self.assertEqual(self.picked(), {"J"})
        self.drag("J")
        self.assertEqual(self.picked(), set())

    def test_drag_across_keys_picks_them_all(self):
        self.drag("W", "A", "S", "D")
        self.assertEqual(self.picked(), {"W", "A", "S", "D"})
        self.assertEqual(self.page.set_depth.text(), "Set 4 keys to 1.0 mm")

    def test_a_fast_drag_doesnt_jump_over_keys(self):
        self.drag("Q", "P")
        self.assertEqual(self.picked(), set("QWERTYUIOP"))

    def test_starting_on_a_picked_key_unpicks(self):
        self.page.pick(list("QWERTY"))
        self.drag("W", "R")
        self.assertEqual(self.picked(), {"Q", "T", "Y"})

    def test_keys_without_sensors_stay_out(self):
        self.drag("1", "F1")
        self.assertEqual(self.picked(), {"1"})
        self.assertFalse(self.page.caps["F1"].isChecked())

    def test_moving_without_a_press_does_nothing(self):
        self.event(self.page.caps["W"], QEvent.MouseMove, self.center("E"), Qt.NoButton)
        self.assertEqual(self.picked(), set())

    def test_right_button_doesnt_pick(self):
        self.event(self.page.caps["W"], QEvent.MouseButtonPress, self.center("W"), Qt.RightButton)
        self.assertEqual(self.picked(), set())

    def test_box_from_an_empty_spot(self):
        empty = self.page.caps["UP"].geometry().center()  # no sensor, so it counts as empty
        self.event(self.plate, QEvent.MouseButtonPress, empty)
        self.event(self.plate, QEvent.MouseMove, self.center("H"), Qt.NoButton)
        self.assertFalse(self.plate.band.isHidden())
        self.assertLessEqual({"H", "J", "ENTER", "RSHIFT"}, self.picked())
        self.assertNotIn("F", self.picked())
        self.event(self.plate, QEvent.MouseButtonRelease, self.center("H"))
        self.assertTrue(self.plate.band.isHidden())

    def test_a_box_keeps_what_was_picked_and_can_shrink(self):
        self.page.pick(["Q"])
        start = self.page.caps["UP"].geometry().center()
        self.event(self.plate, QEvent.MouseButtonPress, start)
        self.event(self.plate, QEvent.MouseMove, self.center("H"), Qt.NoButton)
        self.assertIn("H", self.picked())
        self.event(self.plate, QEvent.MouseMove, self.center("L"), Qt.NoButton)
        self.assertNotIn("H", self.picked())
        self.assertIn("Q", self.picked())
        self.event(self.plate, QEvent.MouseButtonRelease, self.center("L"))

    def test_a_press_on_an_empty_spot_changes_nothing(self):
        self.page.pick(["Q"])
        point = self.page.caps["UP"].geometry().center()
        self.event(self.plate, QEvent.MouseButtonPress, point)
        self.event(self.plate, QEvent.MouseButtonRelease, point)
        self.assertEqual(self.picked(), {"Q"})

    def test_a_press_on_a_key_without_a_sensor_reaches_the_plate(self):
        cap = self.page.caps["UP"]
        point = cap.geometry().center()
        self.event(cap, QEvent.MouseButtonPress, point)
        self.assertEqual(self.plate.drag_mode, "box")
        self.plate.end_drag()


class BindingsMode(unittest.TestCase):
    def setUp(self):
        self.device = FakeKeyboard()
        self.page = gui.KeyboardPage(self.device, Inline(), yes)
        self.page.refresh()
        self.page.mode_buttons["bindings"].click()

    def layer(self, name):
        self.page.bind_layer.setCurrentIndex(self.page.bind_layer.findData(name))

    def test_every_bindable_key_can_be_picked(self):
        caps = self.page.caps
        for name in ("ESC", "F5", "KP7", "KP_ENTER", "UP", "CAPS", "SS"):
            self.assertFalse(caps[name].inert, name)
        self.assertEqual(caps["CAPS"].detail, "Esc")
        self.assertTrue(caps["CAPS"].marked)
        self.assertEqual(caps["A"].detail, "")
        self.assertIsNone(caps["A"].fill)
        QTest.mouseClick(caps["KP7"], Qt.LeftButton)
        self.assertEqual(self.page.selected_keys(), ["KP7"])
        self.assertEqual(self.device.calls, [])

    def test_each_layer_shows_its_own(self):
        self.layer("ss")
        self.assertEqual(self.page.caps["Q"].detail, "Fn")
        self.assertFalse(self.page.caps["Q"].marked)
        self.assertEqual(self.page.caps["J"].detail, "Play")
        self.assertEqual(self.page.bind_title.text(), "Pick a key to rebind")
        QTest.mouseClick(self.page.caps["J"], Qt.LeftButton)
        self.assertEqual(self.page.bind_title.text(), "J, while the SteelSeries key is held")
        self.assertTrue(self.page.caps["J"].marked)
        self.layer("deep")
        self.assertTrue(self.page.caps["ESC"].inert)
        self.assertFalse(self.page.caps["W"].inert)

    def test_your_bindings_lists_changes_with_a_remove_each(self):
        def texts():
            out = []
            for i in range(self.page.changed_list.count()):
                item = self.page.changed_list.itemAt(i)
                if item.widget():
                    out.append(item.widget().text())
                elif item.layout():
                    out += [item.layout().itemAt(j).widget().text() for j in range(item.layout().count()) if item.layout().itemAt(j).widget()]
            return out
        self.assertIn("Remove", texts())
        self.assertIn("Caps: Esc", texts())
        self.layer("ss")
        self.assertTrue(any(t.startswith("J") for t in texts()), texts())
        self.assertFalse(any(t.startswith("Q") for t in texts()), texts())  # the keyboard's own SS+Q isn't a change
        row = next(self.page.changed_list.itemAt(i).layout() for i in range(self.page.changed_list.count())
                   if self.page.changed_list.itemAt(i).layout())
        row.itemAt(row.count() - 1).widget().click()
        self.assertEqual(self.device.calls[-1][0], "bind")
        self.assertEqual(self.device.calls[-1][2:], ("default", "ss"))

    def test_ss_layer_lights_can_be_synced(self):
        self.page.ss_lights.click()
        self.assertEqual(self.device.calls[-1], ("sync_ss_lights",))

    def test_bind_a_key(self):
        QTest.mouseClick(self.page.caps["CAPS"], Qt.LeftButton)
        self.assertEqual(self.page.bind_picker.action_text(), "esc")
        self.page.bind_picker.set_action("ctrl+c")
        self.page.bind_apply.click()
        self.assertEqual(self.device.calls[0], ("bind", ["CAPS"], "ctrl+c", "base"))
        self.page.bind_reset.click()
        self.assertEqual(self.device.calls[1], ("bind", ["CAPS"], "default", "base"))

    def test_steelseries_key_only_while_held(self):
        QTest.mouseClick(self.page.caps["SS"], Qt.LeftButton)
        self.assertFalse(self.page.bind_apply.isEnabled())
        self.assertIn("while held", self.page.bind_note.text())
        self.layer("ss")
        self.assertTrue(self.page.bind_apply.isEnabled())
        self.page.bind_picker.set_action("media:mute")
        self.page.bind_apply.click()
        self.assertEqual(self.device.calls[0], ("bind", ["SS"], "media:mute", "ss"))

    def test_controls_off_the_plate(self):
        QTest.mouseClick(self.page.caps["A"], Qt.LeftButton)
        self.page.bind_other.setCurrentIndex(self.page.bind_other.findData("WHEEL_UP"))
        self.page.bind_other.activated.emit(self.page.bind_other.currentIndex())
        self.assertEqual(self.page.selected_keys(), [])
        self.assertIn("Wheel up", self.page.bind_title.text())
        self.assertEqual(self.page.bind_picker.action_text(), "media:volume-up")
        self.page.bind_picker.set_action("media:next")
        self.page.bind_apply.click()
        self.assertEqual(self.device.calls[0], ("bind", ["WHEEL_UP"], "media:next", "base"))
        self.layer("deep")
        self.assertFalse(self.page.bind_apply.isEnabled())

    def test_undo_puts_back_what_was_there_when_it_opened(self):
        self.assertFalse(self.page.bind_undo.isEnabled())
        changed = json.loads(json.dumps(self.device.data["bindings"]))
        changed["layers"]["base"]["CAPS"] = {"action": "capslock", "raw": "5139000000", "default": True}
        changed["layers"]["ss"]["K"] = {"action": "f13", "raw": "5168000000", "default": False}
        self.device.data = {**self.device.data, "bindings": changed}
        self.page.refresh()
        self.assertTrue(self.page.bind_undo.isEnabled())
        self.page.bind_undo.click()
        self.assertEqual(self.device.calls[0], ("restore_bindings", [("CAPS", "esc", "base"), ("K", "none", "ss")]))

    def test_where_the_deeper_press_starts(self):
        self.assertFalse(self.page.deep_row.isVisibleTo(self.page))
        self.layer("deep")
        self.assertTrue(self.page.deep_row.isVisibleTo(self.page))
        QTest.mouseClick(self.page.caps["W"], Qt.LeftButton)
        self.assertIn("starts at 3.0 mm", self.page.bind_note.text())
        self.assertEqual(self.page.deep_depth.currentData(), 3.0)
        QTest.mouseClick(self.page.caps["W"], Qt.LeftButton)
        QTest.mouseClick(self.page.caps["A"], Qt.LeftButton)
        self.assertIn("very bottom", self.page.bind_note.text())
        self.page.deep_depth.setCurrentIndex(self.page.deep_depth.findData(2.6))
        self.page.deep_set.click()
        self.assertEqual(self.device.calls[0], ("set_deep", ["A"], 2.6))

    def test_back_to_actuation(self):
        self.page.mode_buttons["actuation"].click()
        self.assertTrue(self.page.caps["ESC"].inert)
        self.assertEqual(self.page.caps["W"].detail, "1.0 R")

    def test_an_older_apex_analog_hides_bindings(self):
        self.device.data = {**self.device.data, "bindings": None}
        self.page.refresh()
        self.assertFalse(self.page.mode_buttons["bindings"].isEnabled())
        self.assertEqual(self.page.mode, "actuation")


class FakeHeadset:
    def __init__(self):
        self.calls = []
        self.fail = None
        self.data = {"status": {"ready": True, "connected": True, "rf_status": 1, "battery_mv": 3950, "battery_status": 1, "mic_muted": False,
                                "mic_db": -5, "sidetone_on": True, "sidetone_db": -18, "chat": 70, "game": 30, "mode": 1},
                     "versions": {"transmitter": "1.1", "headset": "1.4"}, "sidetone": -18, "mic": -5, "surround": False,
                     "preset": 1, "bands": [0.0] * 10, "sleep": 1800}

    def read(self):
        if self.fail:
            raise self.fail
        return self.data

    def status(self):
        return self.read()["status"]

    def call(self, method, *args):
        if self.fail:
            raise self.fail
        self.calls.append((method, *args))


class HeadsetPageTest(unittest.TestCase):
    def setUp(self):
        self.device = FakeHeadset()
        self.page = gui.HeadsetPage(self.device, Inline(), yes)
        self.page.refresh()

    def test_loading_shows_everything_and_sends_nothing(self):
        self.assertEqual(self.device.calls, [])
        self.assertEqual(self.page.link.text(), "Connected")
        self.assertIn("3.95 V", self.page.battery.text())
        self.assertIn("chat 70, game 30", self.page.chatmix.text())
        self.assertEqual(self.page.sidetone.currentData(), "low")
        self.assertEqual(self.page.mic.value(), -5)
        self.assertEqual(self.page.sleep.currentData(), 30)
        self.assertEqual(self.page.preset.currentText(), "Bass boost")

    def test_each_control_sends_its_setting(self):
        page = self.page
        page.sidetone.setCurrentIndex(page.sidetone.findData("high"))
        page.sidetone.activated.emit(page.sidetone.currentIndex())
        page.mic.setValue(-12)
        page.mic.sliderReleased.emit()
        page.surround.click()
        page.sleep.setCurrentIndex(page.sleep.findData(0))
        page.sleep.activated.emit(page.sleep.currentIndex())
        page.preset.setCurrentIndex(page.preset.findData(3))
        page.preset.activated.emit(page.preset.currentIndex())
        page.bands[0].setValue(25)
        page.use_bands.click()
        self.assertEqual(self.device.calls, [("set_sidetone", -3), ("set_mic", -12), ("set_surround", True), ("set_sleep", 0),
                                             ("set_eq_preset", 3), ("set_eq_bands", [2.5] + [0.0] * 9), ("set_eq_preset", 4)])

    def test_save_asks_first(self):
        page = gui.HeadsetPage(self.device, Inline(), no)
        page.refresh()
        page.save_button.click()
        self.assertEqual(self.device.calls, [])
        self.page.save_button.click()
        self.assertEqual(self.device.calls, [("save",)])

    def test_headset_off(self):
        self.device.data["status"] = {**self.device.data["status"], "connected": False}
        self.page.refresh()
        self.assertIn("isn't connected", self.page.link.text())
        self.assertEqual(self.page.battery.text(), "")

    def test_no_transmitter(self):
        self.device.fail = gui.arctis.ArctisError("No Arctis 9 transmitter found. Is it plugged in?")
        self.page.refresh()
        self.assertEqual(self.page.connection.text(), "Headset not reachable")
        self.assertFalse(self.page.content.isEnabled())
        self.assertTrue(self.page.retry.isVisibleTo(self.page))

    def test_window_with_other_devices_never_opens_a_real_headset(self):
        window = gui.MainWindow(FakeMouse(), FakeKeyboard(), (Inline(), Inline()), yes)
        self.assertIsInstance(window.headset_page.device, gui.NoHeadset)
        self.assertEqual(window.tabs.tabText(2), "Headset")


class FakeGearDevices:
    def __init__(self):
        self.calls = []
        families = gui.gear.load_families()
        self.covered = {pid: families[pid] for pid in ("1824", "2202")}
        self.found = [
            {"id": "1038:1824", "product": "SteelSeries Rival 3", "name": "Rival 3", "kind": "mouse", "part": "device", "support": "experimental"},
            {"id": "1038:1640", "product": "Apex Pro Gen 3", "name": "Apex Pro (2024)", "kind": "keyboard", "part": "device", "support": "full"},
            {"id": "1038:9999", "product": None, "name": None, "kind": None, "part": None, "support": None},
        ]

    def scan(self):
        return self.found

    def families(self):
        return self.covered

    def remembered(self, pid):
        return {"polling_rate": "500"} if pid == "1824" else {}

    def status(self, pid):
        return {"status": {}, "battery": {"percent": 70, "charging": True}, "firmware": "1.2"}

    def set(self, pid, name, value):
        self.calls.append(("set", pid, name, value))

    def save(self, pid):
        self.calls.append(("save", pid))


class DevicesPageTest(unittest.TestCase):
    def setUp(self):
        self.devices = FakeGearDevices()
        self.page = gui.DevicesPage(self.devices, Inline(), yes)
        self.page.refresh()

    def test_lists_what_is_plugged_in(self):
        self.assertEqual(self.page.support_note(self.devices.found[0], "1824"), "Settings here, experimental")
        self.assertEqual(self.page.support_note(self.devices.found[1], "1640"), "On the Keyboard tab")
        self.assertEqual(self.page.support_note(self.devices.found[2], "9999"), "Not supported yet")

    def test_settings_from_the_definition(self):
        self.page.show_device(self.devices.found[0])
        self.assertIn("polling_rate", self.page.inputs)
        box = self.page.inputs["polling_rate"]
        self.assertEqual(box.currentText(), "500")
        box.setCurrentText("1000")
        self.page.apply("1824", "polling_rate", box, "choice")
        self.page.inputs["sensitivity"].setText("400,800")
        self.page.apply("1824", "sensitivity", self.page.inputs["sensitivity"], "dpi_stages")
        self.assertEqual(self.devices.calls, [("set", "1824", "polling_rate", "1000"), ("set", "1824", "sensitivity", "400,800")])

    def test_empty_values_are_not_sent(self):
        self.page.show_device(self.devices.found[0])
        self.page.inputs["sensitivity"].setText("")
        self.page.apply("1824", "sensitivity", self.page.inputs["sensitivity"], "dpi_stages")
        self.assertEqual(self.devices.calls, [])

    def test_status_and_save(self):
        self.page.show_device(self.devices.found[0])
        self.page.read_status("1824")
        self.assertIn("battery 70%, charging", self.page.readout.text())
        self.page.save("1824")
        self.assertEqual(self.devices.calls, [("save", "1824")])

    def test_headset_equalizer_offers_presets(self):
        self.page.show_device({"id": "1038:2202", "product": "Arctis Nova 7", "name": "Arctis Nova 7", "kind": "headset", "part": "base station", "support": "experimental"})
        eq = self.page.inputs["equalizer"]
        self.assertGreaterEqual(eq.findText("bass"), 0)

    def test_others_point_to_their_tab(self):
        self.page.show_device(self.devices.found[1])
        self.assertEqual(self.page.inputs, {})


class WholeKeyboard(unittest.TestCase):
    def setUp(self):
        self.page = gui.KeyboardPage(FakeKeyboard(), Inline(), yes)
        self.plate = self.page.plate

    def test_numpad_is_there(self):
        for name in ("KP_NUM", "KP_DIV", "KP_MUL", "KP_SUB", "KP_ADD", "KP_ENTER", "KP_DOT", *(f"KP{n}" for n in range(10))):
            self.assertIn(name, self.page.caps)
        self.assertEqual(self.page.caps["KP7"].title, "7")
        self.assertGreater(self.page.caps["KP_ADD"].height(), self.page.caps["KP7"].height() * 1.5)
        self.assertGreater(self.page.caps["KP0"].width(), self.page.caps["KP1"].width() * 1.5)

    def test_every_part_fits_and_nothing_overlaps(self):
        parts = [*self.page.caps.values(), self.plate.screen, self.plate.wheel, self.plate.media]
        for part in parts:
            self.assertTrue(self.plate.rect().contains(part.geometry()), getattr(part, "title", part))
        for i, a in enumerate(parts):
            for b in parts[i + 1:]:
                self.assertFalse(a.geometry().intersects(b.geometry()), f"{getattr(a, 'title', a)} and {getattr(b, 'title', b)}")

    def test_the_real_corner(self):
        screen, wheel, media = self.plate.screen.geometry(), self.plate.wheel.geometry(), self.plate.media.geometry()
        self.assertLess(screen.right(), wheel.left())
        self.assertLess(wheel.bottom(), media.top())
        self.assertEqual(wheel.left(), media.left())
        self.assertEqual(screen.left(), self.page.caps["KP_NUM"].x())
        self.assertEqual(wheel.left(), self.page.caps["KP_SUB"].x())

    def test_the_screen_fits_a_panels_worth_of_pixels(self):
        self.assertGreaterEqual(self.plate.screen.width(), gui.SCREEN_WIDTH)
        self.assertGreaterEqual(self.plate.screen.height(), gui.SCREEN_HEIGHT)

    def test_fits_the_window(self):
        self.assertLessEqual(self.plate.width() + 52, gui.MainWindow(FakeMouse(), FakeKeyboard(), (Inline(), Inline()), yes).minimumWidth())


class FakeOled:
    def __init__(self):
        self.calls = []
        self.state = {"ok": True, "fb": None, "source": "idle", "blanked": False, "blank_reason": None, "released": False, "forced_mode": None}
        self.fail = None

    def frame(self):
        if self.fail:
            raise self.fail
        return self.state

    def set_mode(self, mode):
        self.calls.append(("mode", mode))

    def toggle_power(self):
        self.calls.append(("power",))

    def show_message(self, text):
        self.calls.append(("message", text))


class Screen(unittest.TestCase):
    def setUp(self):
        self.oled = FakeOled()
        self.page = gui.KeyboardPage(FakeKeyboard(), Inline(), yes, oled=self.oled)
        self.page.poll_screen()

    def test_pixels_land_in_the_right_place(self):
        fb = bytearray(640)
        fb[1 * 128 + 3] = 1 << 2  # page 1, column 3, bit 2 = row 10
        image = gui.oled_image(bytes(fb))
        self.assertEqual((image.width(), image.height()), (128, 40))
        self.assertEqual(image.pixelColor(3, 10).value(), 255)
        self.assertEqual(image.pixelColor(3, 9).value(), 0)
        self.assertEqual(image.pixelColor(4, 10).value(), 0)

    def test_shows_on_the_plate_and_the_panel(self):
        self.oled.state["fb"] = base64.b64encode(bytes([255]) * 640).decode()
        self.page.poll_screen()
        self.assertIsNotNone(self.page.plate.screen.image)
        self.assertIsNotNone(self.page.screen_zoom.image)
        self.assertFalse(self.page.plate.screen.grab().isNull())
        self.assertEqual(self.page.screen_state.text(), "Showing system stats.")

    def test_the_enlarged_view_shows_the_whole_panel(self):
        view = self.page.screen_zoom
        self.assertGreaterEqual(view.width(), gui.SCREEN_WIDTH * view.zoom)
        self.assertGreaterEqual(view.height(), gui.SCREEN_HEIGHT * view.zoom)

    def test_a_dark_panel_looks_dark(self):
        self.oled.state.update(fb=base64.b64encode(bytes([255]) * 640).decode())
        self.page.poll_screen()
        self.oled.state.update(blanked=True, blank_reason="away")
        self.page.poll_screen()
        self.assertIsNone(self.page.plate.screen.image)
        self.assertEqual(self.page.screen_state.text(), "Dark until you're back.")
        self.assertEqual(self.page.screen_power.text(), "Turn screen off")

    def test_turned_off_by_hand(self):
        self.oled.state.update(blanked=True, blank_reason="manual")
        self.page.poll_screen()
        self.assertEqual(self.page.screen_power.text(), "Turn screen on")
        self.page.screen_power.click()
        self.assertEqual(self.oled.calls, [("power",)])

    def test_a_pinned_mode_says_so(self):
        self.oled.state["forced_mode"] = "lyrics"
        self.page.poll_screen()
        self.assertEqual(self.page.screen_mode.currentData(), "lyrics")
        self.assertIn("Pinned to lyrics", self.page.screen_state.text())

    def test_showing_the_pinned_mode_sends_nothing(self):
        self.oled.state["forced_mode"] = "lyrics"
        self.page.poll_screen()
        self.assertEqual(self.oled.calls, [])

    def test_picking_a_mode(self):
        self.page.screen_mode.setCurrentIndex(self.page.screen_mode.findData("game"))
        self.page.screen_mode.activated.emit(self.page.screen_mode.currentIndex())
        self.assertEqual(self.oled.calls, [("mode", "game")])

    def test_a_message(self):
        self.page.screen_text.setText("  hello  ")
        self.page.screen_send.click()
        self.assertEqual(self.oled.calls, [("message", "hello")])
        self.assertEqual(self.page.screen_text.text(), "")

    def test_an_empty_message_sends_nothing(self):
        self.page.screen_text.setText("   ")
        self.page.screen_send.click()
        self.assertEqual(self.oled.calls, [])

    def test_a_program_holding_the_panel(self):
        self.oled.state["released"] = True
        self.page.poll_screen()
        self.assertIn("Another program", self.page.screen_state.text())

    def test_an_older_daemon_says_how_to_fix_it(self):
        self.oled.state.update(outdated=True)
        self.page.poll_screen()
        self.assertIn("Restart apex-oledd", self.page.screen_state.text())

    def test_daemon_gone_and_back(self):
        self.oled.fail = gui.KeyboardError("apex-oledd isn't running, so the screen can't be reached.")
        self.page.poll_screen()
        self.assertFalse(self.page.screen_mode.isEnabled())
        self.assertIn("isn't running", self.page.screen_state.text())
        self.oled.fail = None
        self.page.poll_screen()
        self.assertTrue(self.page.screen_mode.isEnabled())

    def test_polls_dont_pile_up(self):
        class Holding:
            def __init__(self):
                self.jobs = []

            def submit(self, call, done=None, failed=None):
                self.jobs.append((call, done))

        held = Holding()
        page = gui.KeyboardPage(FakeKeyboard(), Inline(), yes, oled=self.oled, oled_runner=held)
        page.poll_screen()
        page.poll_screen()
        self.assertEqual(len(held.jobs), 1)
        call, done = held.jobs[0]
        done(call())
        page.poll_screen()
        self.assertEqual(len(held.jobs), 2)

    def test_no_daemon_configured(self):
        page = gui.KeyboardPage(FakeKeyboard(), Inline(), yes)
        page.poll_screen()
        self.assertFalse(page.screen_mode.isEnabled())


class OledSocket(unittest.TestCase):
    def serve(self, replies):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        path = os.path.join(d.name, "s.sock")
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(path)
        server.listen(4)
        self.addCleanup(server.close)
        seen = []

        def run():
            for reply in replies:
                conn, _ = server.accept()
                with conn:
                    seen.append(json.loads(conn.recv(4096)))
                    conn.sendall((json.dumps(reply) + "\n").encode())

        threading.Thread(target=run, daemon=True).start()
        return gui.OledDevice(path), seen

    def test_frame(self):
        device, seen = self.serve([{"ok": True, "fb": "AA==", "source": "idle"}])
        self.assertEqual(device.frame()["source"], "idle")
        self.assertEqual(seen, [{"cmd": "frame"}])

    def test_an_older_daemon_falls_back_to_status(self):
        device, seen = self.serve([{"ok": False, "error": "unknown command"}, {"ok": True, "source": "lyrics"}])
        state = device.frame()
        self.assertTrue(state["outdated"])
        self.assertEqual(state["source"], "lyrics")
        self.assertEqual([m["cmd"] for m in seen], ["frame", "status"])

    def test_commands(self):
        device, seen = self.serve([{"ok": True}] * 3)
        device.set_mode("lyrics")
        device.toggle_power()
        device.show_message("hi")
        self.assertEqual(seen[0], {"cmd": "action", "action": "mode:lyrics"})
        self.assertEqual(seen[1], {"cmd": "action", "action": "blank"})
        self.assertEqual(seen[2]["lines"], ["hi"])

    def test_no_daemon(self):
        with self.assertRaisesRegex(gui.KeyboardError, "isn't running"):
            gui.OledDevice("/nonexistent/s.sock").frame()


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

    APEX_ANALOG = os.path.join(os.path.dirname(os.path.realpath(__file__)), "..", "apex-analog", "apex_analog.py")

    def test_apex_analog_understands_every_key_we_send(self):
        spec = importlib.util.spec_from_file_location("apex_analog", self.APEX_ANALOG)
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
        device = self.script('printf "%s " "$@" >> "$0.log"\n')
        device.bind(["`", "CAPS"], "esc", "base")
        device.restore_bindings([("K", "none", "ss")])
        device.set_deep(["W", ","], 3.0)
        with open(device.exe + ".log") as f:
            self.assertEqual(f.read(), "bind GRAVE esc --layer base bind CAPS esc --layer base bind K none --layer ss actuation W,COMMA 3 --deep ")

    def test_report_is_just_the_summary(self):
        device = self.script('printf "6909 presses.\\nTry 1.4 mm.\\n\\nkey  presses\\nSPACE 1406\\n"\n')
        self.assertEqual(device.report("profile"), "6909 presses.\nTry 1.4 mm.")

    def test_real_shape_read(self):
        device = self.script('case "$1" in\ninfo) echo \'{"firmware":"0416","keys":{}}\';;\n'
                             'status) echo "Daemon running.";;\npreset) echo one; echo two;;\nesac\n')
        data = device.read()
        self.assertEqual(data["presets"], ["one", "two"])
        self.assertEqual(data["jobs"], [])


class Stream(unittest.TestCase):
    def script(self, body):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        path = os.path.join(d.name, "fake-stream")
        with open(path, "w") as f:
            f.write("#!/bin/sh\n" + body)
        os.chmod(path, os.stat(path).st_mode | stat.S_IEXEC)
        return path

    def wait(self, until, seconds=3):
        end = time.monotonic() + seconds
        while time.monotonic() < end and not until():
            app.processEvents()
            time.sleep(0.005)
        return until()

    def test_frames_arrive_and_only_changes_are_sent(self):
        path = self.script('echo "{}"\necho "{}"\necho \'{"W": 1.5}\'\nsleep 5\n')
        stream = gui.DepthStream([path])
        seen = []
        stream.changed.connect(seen.append)
        stream.start()
        self.assertTrue(self.wait(lambda: seen))
        self.assertEqual(seen, [{"W": 1.5}])
        stream.stop()
        self.assertEqual(seen, [{"W": 1.5}, {}])
        self.assertIsNone(stream.process)

    def test_a_half_written_line_waits_for_the_rest(self):
        path = self.script('printf \'{"W": 1\'\nsleep 0.1\nprintf \'.5}\\n\'\nsleep 5\n')
        stream = gui.DepthStream([path])
        seen = []
        stream.changed.connect(seen.append)
        stream.start()
        self.assertTrue(self.wait(lambda: seen))
        self.assertEqual(seen, [{"W": 1.5}])
        stream.stop()

    def test_garbage_is_ignored(self):
        path = self.script('echo "not json"\necho \'{"W": 2.0}\'\nsleep 5\n')
        stream = gui.DepthStream([path])
        seen = []
        stream.changed.connect(seen.append)
        stream.start()
        self.assertTrue(self.wait(lambda: {"W": 2.0} in seen))
        stream.stop()

    def test_a_dead_stream_lets_go_of_held_keys_and_retries(self):
        path = self.script('echo \'{"W": 2.0}\'\n')
        stream = gui.DepthStream([path])
        stream.RETRY_MS = 10
        seen = []
        stream.changed.connect(seen.append)
        stream.start()
        self.assertTrue(self.wait(lambda: seen.count({"W": 2.0}) >= 2), seen)
        self.assertIn({}, seen)
        stream.stop()

    def test_stopped_means_stopped(self):
        path = self.script('echo \'{"W": 2.0}\'\n')
        stream = gui.DepthStream([path])
        stream.RETRY_MS = 10
        stream.start()
        stream.stop()
        for _ in range(10):
            app.processEvents()
            time.sleep(0.005)
        self.assertIsNone(stream.process)

    def test_not_installed_is_quiet(self):
        stream = gui.DepthStream(["/nonexistent/apex-analog"])
        stream.start()
        self.assertIsNone(stream.process)
        stream.stop()

    def test_runs_only_while_the_page_shows(self):
        path = self.script('sleep 30\n')
        device = FakeKeyboard()
        device.stream_command = lambda: [path]
        page = gui.KeyboardPage(device, Inline(), yes)
        page.refresh()
        page.show()
        app.processEvents()
        self.assertIsNotNone(page.stream.process)
        page.hide()
        app.processEvents()
        self.assertIsNone(page.stream.process)
        self.assertEqual(device.calls, [])


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
    def test_fits_and_has_every_page(self):
        window = gui.MainWindow(FakeMouse(), FakeKeyboard(), (Inline(), Inline()), yes)
        window.load()
        self.assertEqual([window.tabs.tabText(i) for i in range(window.tabs.count())], ["Mouse", "Keyboard", "Headset", "Other devices"])
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


class AppExit(unittest.TestCase):
    def test_the_real_app_closes_cleanly(self):
        """In its own process, because a crash on shutdown only shows in the exit code."""
        script = """
import sys
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication
import gui


class NoDevice:
    def read(self):
        raise gui.KeyboardError("no device")
    battery = frame = read


gui.MouseDevice = gui.KeyboardDevice = gui.OledDevice = NoDevice
real_exec = QApplication.exec


def timed(*_):
    def close():
        for widget in QApplication.topLevelWidgets():
            widget.close()
        QApplication.quit()
    QTimer.singleShot(500, close)
    return real_exec()


QApplication.exec = staticmethod(timed)
sys.exit(gui.main(["steelmc"]))
"""
        run = subprocess.run([sys.executable, "-X", "faulthandler", "-c", script], cwd=os.path.dirname(os.path.realpath(__file__)),
                             env={**os.environ, "QT_QPA_PLATFORM": "offscreen"}, capture_output=True, text=True, timeout=60)
        self.assertEqual(run.returncode, 0, run.stderr[-2000:])


if __name__ == "__main__":
    result = unittest.main(exit=False).result
    # Leftover test widgets can crash Python's shutdown on PySide6 6.12. The app itself has its own test above.
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(0 if result.wasSuccessful() else 1)
