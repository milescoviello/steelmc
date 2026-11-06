#!/usr/bin/env python3
"""Render the GUI to PNG files without putting anything on screen.

    python3 screenshots.py OUTDIR          sample data, no devices touched
    python3 screenshots.py OUTDIR --real   reads from the real mouse and keyboard, changes nothing
"""
import base64
import os
import sys

os.environ["QT_QPA_PLATFORM"] = "offscreen"  # never on screen, whatever the session is

from PySide6.QtGui import QColor, QFont, QImage, QPainter
from PySide6.QtWidgets import QApplication

import gui
import aerox


class Inline:
    """Runs calls straight away, so a screenshot has its data."""

    def submit(self, call, done=None, failed=None):
        try:
            value = call()
        except (aerox.AeroxError, gui.KeyboardError, OSError) as e:
            if failed:
                failed(e)
        else:
            if done:
                done(value)


def sample_mouse():
    records = bytearray(b"".join(aerox.DEFAULTS))
    records[25:30] = bytes.fromhex("0600000000")
    records[45:50] = aerox.parse_action("alt+tab")
    records[65:70] = bytes.fromhex("302d000000")
    dpi = {"count": 5, "current": 0, "levels": [4, 9, 13, 27, 38], "dpi": [400, 800, 1200, 2400, 3200]}
    return {
        "connection": "dongle", "firmware": "1.2.4",
        "battery": {"level": 5, "percent": 20, "charging": False},
        "polling": {"index": 0, "hz": 1000}, "dpi": dpi, "sleep_ms": 300000, "low_power": False,
        "lighting": {"brightness": 15, "idle_brightness": 1, "smart": True, "idle_timeout_ms": 30000},
        "reactive": {"enabled": True, "random": False, "color": "ffffff"},
        "buttons": aerox.decode_buttons(records),
        "backup": {},
    }


NAMES = ["`", *"1234567890-=", "TAB", *"QWERTYUIOP[]\\", "CAPS", *"ASDFGHJKL;'", "ENTER", "LSHIFT", *"ZXCVBNM,./", "RSHIFT",
         "LCTRL", "LGUI", "LALT", "SPACE", "RALT", "RGUI", "SS", "RCTRL", "BKSP"]


def sample_keyboard():
    keys = {n: {"press": 1.89, "release": 1.8, "mode": 0} for n in NAMES}
    for n in "WASD":
        keys[n] = {"press": 1.08, "release": 1.0, "mode": 2}
    keys["SPACE"] = {"press": 3.0, "release": 3.0, "mode": 0}
    return {
        "info": {"firmware": "0416", "keys": keys},
        "jobs": [{"name": "lights", "on": True, "command": "lights --keep", "age": "135 min"},
                 {"name": "recording", "on": True, "command": "record", "age": "135 min"}],
        "presets": ["gaming", "typing"],
    }


def sample_screen():
    """A plausible frame for the docs, packed the way the panel takes it."""
    image = QImage(128, 40, QImage.Format_Grayscale8)
    image.fill(0)
    p = QPainter(image)
    p.setPen(QColor(255, 255, 255))
    font = QFont("Noto Sans")
    font.setPixelSize(11)
    font.setStyleStrategy(QFont.NoAntialias)
    p.setFont(font)
    for y, label, value, used in ((1, "CPU", "12%  48C", .12), (14, "GPU", "3%  41C", .03), (27, "RAM", "11 GB", .36)):
        p.drawText(2, y + 10, label)
        p.drawRect(26, y + 2, 50, 7)
        p.fillRect(26, y + 2, round(50 * used), 7, QColor(255, 255, 255))
        p.drawText(80, y + 10, value)
    p.end()
    fb = bytearray(640)
    for x in range(128):
        for y in range(40):
            if image.pixelColor(x, y).value() >= 128:
                fb[y // 8 * 128 + x] |= 1 << (y % 8)
    return base64.b64encode(bytes(fb)).decode()


class FakeOled:
    def frame(self):
        return {"ok": True, "fb": sample_screen(), "source": "idle", "blanked": False, "blank_reason": None, "released": False, "forced_mode": None}


class FakeMouse:
    def read(self):
        return sample_mouse()


class FakeKeyboard:
    def read(self):
        return sample_keyboard()


def render(window, path):
    window.resize(window.minimumSize().expandedTo(window.sizeHint()))
    window.show()
    QApplication.processEvents()
    window.grab().save(path)
    print(path, window.width(), window.height())


def main():
    out = sys.argv[1]
    real = "--real" in sys.argv
    os.makedirs(out, exist_ok=True)
    app = QApplication([])
    gui.style_app(app)
    inline = (Inline(), Inline())
    window = gui.MainWindow(None if real else FakeMouse(), None if real else FakeKeyboard(), inline, confirm=lambda *a: False,
                            oled=None if real else FakeOled())
    window.load()
    prefix = "real-" if real else "sample-"
    window.mouse_page.poller.stop()
    render(window, os.path.join(out, prefix + "mouse.png"))
    window.tabs.setCurrentIndex(1)
    window.keyboard_page.screen_timer.stop()
    window.keyboard_page.poll_screen()
    window.keyboard_page.pick(["W", "A", "S", "D"])
    if not real:
        window.keyboard_page.report.setText("61 keys checked. Rest offset 10 to 32 counts, noise up to 1.8, typical range 2488.\nNothing looks wrong.")
        window.keyboard_page.show_depths({"F": 3.2, "J": 0.7})
    render(window, os.path.join(out, prefix + "keyboard.png"))
    window.keyboard_page.show_depths({})
    if not real:
        page = window.keyboard_page
        page.pick([])
        page.plate.begin_drag(page.caps["UP"].geometry().center())
        page.plate.continue_drag(page.caps["F"].geometry().center())
        render(window, os.path.join(out, "sample-keyboard-drag.png"))
        page.plate.end_drag()
    if not real:
        window.tabs.setCurrentIndex(0)
        window.mouse_page.select_button("side 12")
        render(window, os.path.join(out, "sample-mouse-editing.png"))
        window.mouse_page.lost(aerox.MouseUnreachable("The mouse isn't connected. It's off, asleep or out of range: move it and retry."))
        render(window, os.path.join(out, "sample-mouse-asleep.png"))
    window.close()


if __name__ == "__main__":
    main()
