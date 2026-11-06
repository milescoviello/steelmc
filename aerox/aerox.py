#!/usr/bin/env python3
"""aerox: configure a SteelSeries Aerox 9 Wireless from Linux, with no vendor software.

Battery, DPI, polling rate, sleep time, button bindings and lighting, over the mouse's
vendor HID interface. Stops on any firmware but 1.2.4, and can only send the commands
listed below. Changes apply right away but last until the mouse powers off, unless you
run `aerox save`.

    aerox                         status
    aerox battery                 battery
    aerox dpi [LIST] [--stage N]  show or set DPI stages: aerox dpi 400,800,1600 --stage 2
    aerox polling [HZ]            show or set polling rate: 125, 250, 500 or 1000
    aerox sleep [MIN]             show or set sleep time: 0 (off) to 20, in half minutes
    aerox low-power [on|off]      show or set the sensor's low power mode
    aerox buttons                 what each button does
    aerox bind BUTTON ACTION      aerox bind "side 12" alt+tab
    aerox light [options]         brightness, idle dimming, smart lighting
    aerox color ZONE RRGGBB       temporary colour: zone 1, 2, 3 or all, or `aerox color clear`
    aerox reactive [on|off]       reactive lighting on clicks
    aerox save [config|lighting|all]  write to the mouse's flash so changes survive power off
    aerox backup FILE             save the current settings to a file
    aerox restore FILE            put them back (run `aerox save` to make that permanent)
    aerox info                    device and firmware details
    aerox ... --json              the same as JSON, for scripts
"""
import argparse
import fcntl
import glob
import json
import os
import re
import select
import sys
import time

__version__ = "0.2.0"

VENDOR = "00001038"
DONGLE, WIRED = "00001858", "0000185A"
FIRMWARE_BCD = "0124"  # bcdDevice of firmware 1.2.4
FIRMWARE = "1.2.4"
SYSFS = "/sys/class/hidraw"

# The vendor interface's report descriptor, from the firmware and checked on real hardware.
# hidraw numbers move across replugs, so this is how the right node is found.
DESCRIPTOR = bytes.fromhex(
    "06c0ff0901a10106c1ff150026ff00750809f095408102"
    "09f195409102"
    "09f2960002b102c0"
)

# GG's table for this mouse: the DPI is (index + 1) * 100 and the value is what the sensor
# register takes. The steps aren't even, so it can't be replaced with a formula.
DPI_LEVELS = (
    0, 2, 3, 4, 5, 6, 7, 9, 10, 11, 12, 13, 14, 16, 17, 18, 19, 20, 22, 23,
    24, 25, 26, 27, 29, 30, 31, 32, 33, 35, 37, 38, 39, 40, 41, 42, 44, 45, 46, 47,
    48, 50, 51, 52, 53, 54, 56, 57, 58, 59, 60, 62, 63, 64, 65, 66, 68, 69, 70, 71,
    72, 74, 75, 76, 77, 78, 80, 81, 82, 83, 84, 86, 87, 88, 89, 90, 92, 93, 94, 95,
    96, 98, 99, 100, 101, 102, 104, 105, 106, 107, 108, 110, 111, 112, 113, 114, 116, 117, 118, 119,
    120, 122, 123, 124, 125, 126, 128, 129, 130, 131, 132, 134, 135, 136, 137, 138, 140, 141, 142, 143,
    144, 146, 147, 148, 149, 150, 152, 153, 154, 155, 156, 158, 159, 160, 161, 162, 164, 165, 166, 167,
    168, 170, 171, 172, 173, 174, 176, 177, 178, 179, 180, 181, 182, 183, 184, 185, 186, 187, 188, 189,
    191, 192, 194, 195, 196, 197, 198, 199, 201, 202, 203, 204, 205, 207, 208, 209, 210, 211, 213, 214,
)
POLLING_HZ = {0: 1000, 1: 500, 2: 250, 3: 125}
TIMER_STEP_MS = 30000  # GG's timers: off, then half minutes up to 20 minutes
TIMER_MAX_MS = 1200000

ANY = None  # takes no arguments

# Every read this tool can send, with the arguments it may carry. Each handler was read in
# the firmware and touches no flash.
# Bit 7 set does NOT mean "read" on this device: 0x91/0xD1 look like reads but wipe settings.
# So these are lists of known bytes and must never become a range or a mask.
#
# Through the dongle, bit 6 picks the device: clear = the dongle itself, set = forwarded to
# the mouse. Plugged in directly, the mouse ignores bit 6, so the mouse forms still work.
DONGLE_READS = {
    0x90: (0, 1),   # 0 = firmware version, 1 = git hash
    0xBC: ANY,      # radio link up?
}
MOUSE_READS = {
    0xD0: (0, 1),   # 0 = firmware version, 1 = git hash
    0xD2: ANY,      # battery
    0xE3: ANY,      # brightness, idle brightness, smart lighting, idle timeout
    0xE6: ANY,      # reactive lighting
    0xE8: ANY,      # sensor low power mode
    0xE9: ANY,      # sleep time
    0xEA: (0, 1, 2),  # button records, by page
    0xEB: ANY,      # polling rate
    0xED: ANY,      # DPI stages
}
READS = {**DONGLE_READS, **MOUSE_READS}

SAVE = 0x51
SAVE_BLOCKS = {"config": 1, "lighting": 2}  # the two blocks this tool changes


def _is_timer(ms):
    return 0 <= ms <= TIMER_MAX_MS and ms % TIMER_STEP_MS == 0


def _u32(args):
    return int.from_bytes(bytes(args), "little")


RECORD_KINDS = {0x00, *range(0x01, 0x09), 0x30, 0x31, 0x32, 0x33, 0x34, 0x51, 0x61}
VALID_LEVELS = frozenset(DPI_LEVELS)


def _check_dpi(args):
    count, current, *levels = args
    return (len(levels) == 5 and 1 <= count <= 5 and current < count
            and all(v in VALID_LEVELS for v in levels[:count]))


def _check_buttons(args):
    page, *data = args
    size = 40 if page == 2 else 30
    return (page in (0, 1, 2) and len(data) == size
            and all(data[i] in RECORD_KINDS for i in range(0, size, 5)))


def _check_colors(args):
    n, *data = args
    return (1 <= n <= 3 and len(data) == n * 4
            and all(data[i] < 3 for i in range(0, len(data), 4)))


# Every write this tool can send, each with a check on its arguments. None of these reach
# flash on their own: they change RAM until SAVE. The firmware itself checks little: the
# sleep time and button records go straight in, so these checks are the only ones.
WRITES = {
    0x61: _check_colors,                                         # temporary zone colours
    0x62: lambda a: not a,                                       # clear them
    0x63: lambda a: (len(a) == 8 and a[0] < 16 and a[1] < 16 and a[2] < 2 and a[3] == 0
                     and _is_timer(_u32(a[4:]))),                # brightness, idle, smart, timeout
    0x66: lambda a: len(a) == 5 and a[0] < 2 and a[1] < 2,       # reactive lighting
    0x68: lambda a: len(a) == 1 and a[0] < 2,                    # sensor low power mode
    0x69: lambda a: len(a) == 4 and _is_timer(_u32(a)),          # sleep time
    0x6A: _check_buttons,                                        # button records
    0x6B: lambda a: len(a) == 1 and a[0] in POLLING_HZ,          # polling rate
    0x6D: _check_dpi,                                            # all DPI stages
    0x6E: lambda a: len(a) == 1 and a[0] < 5,                    # current DPI stage
    SAVE: lambda a: len(a) == 1 and a[0] in SAVE_BLOCKS.values(),  # erase and write flash
}
ALLOWED = {**READS, **WRITES}

NO_ECHO = {0x90, 0xD0}  # these reply with a bare string
UNREACHABLE = b"\x40\xff"  # the dongle couldn't forward: mouse off, asleep or out of range
REPLY_TIMEOUT = 4.5  # the dongle gives up on a forwarded command after 4 s
RESYNC_TIMEOUT = 15  # how long a polling rate change may take to reconnect
LOCK_TIMEOUT = 20  # long enough to outlast whatever a polling change is doing elsewhere
LOW_BATTERY_LEVEL = 2  # don't erase flash on a nearly flat battery


class AeroxError(Exception):
    """Something the user should read, not a bug."""


class Refused(AeroxError):
    """A command that isn't on the allowlist. Never reaches the device."""


class MouseUnreachable(AeroxError):
    """The dongle is here but can't reach the mouse."""


def check_allowed(cmd, args=()):
    """Raise Refused unless this exact command and these arguments are allowed."""
    if cmd in WRITES:
        if not WRITES[cmd](tuple(args)):
            raise Refused(f"command {cmd:#04x} doesn't accept arguments {list(args)}")
        return
    if cmd not in READS:
        raise Refused(f"command {cmd:#04x} is not on the allowlist")
    allowed = READS[cmd]
    if allowed is ANY:
        if args:
            raise Refused(f"command {cmd:#04x} takes no arguments")
    elif len(args) != 1 or args[0] not in allowed:
        raise Refused(f"command {cmd:#04x} doesn't accept arguments {list(args)}")


def build_frame(cmd, args=()):
    """Report number 0 (the interface has no report IDs), then 64 bytes, zero padded."""
    check_allowed(cmd, args)
    return bytes([0x00, cmd, *args]).ljust(65, b"\0")


def reply_matches(cmd, reply):
    """Is this the answer to cmd? The dongle also pushes unasked `BC <state>` messages."""
    if cmd in NO_ECHO:
        return bool(re.match(rb"[0-9a-f.]+\0", reply))
    if reply[0] != cmd:
        return False
    return cmd != 0xBC or reply[1] in (0, 1)


def find_device(sysfs=SYSFS):
    """Return (node, kind, bcd) for the vendor interface of a connected Aerox 9."""
    for hidraw in sorted(glob.glob(f"{sysfs}/hidraw*")):
        try:
            with open(f"{hidraw}/device/uevent") as f:
                uevent = f.read().upper()
            with open(f"{hidraw}/device/report_descriptor", "rb") as f:
                descriptor = f.read()
        except OSError:
            continue
        if f":{VENDOR}:" not in uevent or descriptor != DESCRIPTOR:
            continue
        if f":{DONGLE}" in uevent:
            kind = "dongle"
        elif f":{WIRED}" in uevent:
            kind = "wired"
        else:
            continue
        path = os.path.realpath(f"{hidraw}/device")
        while path != "/" and not os.path.exists(f"{path}/bcdDevice"):
            path = os.path.dirname(path)
        with open(f"{path}/bcdDevice") as f:
            bcd = f.read().strip()
        return "/dev/" + os.path.basename(hidraw), kind, bcd
    raise AeroxError("No Aerox 9 Wireless found. Is the dongle plugged in?")


def level_to_dpi(level):
    """The DPI for a sensor value, or None if GG's table doesn't list it."""
    try:
        return (DPI_LEVELS.index(level) + 1) * 100
    except ValueError:
        return None


def dpi_to_level(dpi):
    if dpi % 100 or not 100 <= dpi <= len(DPI_LEVELS) * 100:
        raise AeroxError(f"DPI must be a multiple of 100 from 100 to {len(DPI_LEVELS) * 100}, not {dpi}.")
    return DPI_LEVELS[dpi // 100 - 1]


def decode_battery(reply):
    """Bit 7 = charging. The rest is a level from 1 to 21, in 5% steps."""
    level = reply[1] & 0x7F
    return {"level": level, "percent": max(0, min(100, (level - 1) * 5)), "charging": bool(reply[1] & 0x80)}


def decode_polling(reply):
    return {"index": reply[1], "hz": POLLING_HZ.get(reply[1])}


def decode_dpi(reply):
    count, current = reply[1], reply[2]
    levels = list(reply[3:3 + count])
    return {"count": count, "current": current, "levels": levels, "dpi": [level_to_dpi(v) for v in levels]}


def decode_lighting(reply):
    return {
        "brightness": reply[1], "idle_brightness": reply[2], "smart": bool(reply[3]),
        "idle_timeout_ms": int.from_bytes(reply[5:9], "little"),
    }


def decode_reactive(reply):
    return {"enabled": bool(reply[1]), "random": bool(reply[2]), "color": bytes(reply[3:6]).hex()}


KEYS = {0x04 + i: chr(ord("a") + i) for i in range(26)}
KEYS.update({0x1E + i: str((i + 1) % 10) for i in range(10)})
KEYS.update({0x3A + i: f"f{i + 1}" for i in range(12)})
KEYS.update({0x68 + i: f"f{i + 13}" for i in range(12)})
KEYS.update(zip(range(0x28, 0x39), (
    "enter", "esc", "backspace", "tab", "space", "-", "=", "[", "]", "\\", "#", ";", "'", "`", ",", ".", "/",
)))
KEYS.update(zip(range(0x46, 0x53), (
    "printscreen", "scrolllock", "pause", "insert", "home", "pageup", "delete", "end", "pagedown",
    "right", "left", "down", "up",
)))
KEYS[0x39] = "capslock"
KEYS.update(zip(range(0xE0, 0xE8), ("ctrl", "shift", "alt", "meta", "rctrl", "rshift", "ralt", "rmeta")))
KEY_ALIASES = {"control": "ctrl", "win": "meta", "super": "meta", "return": "enter", "escape": "esc",
               "del": "delete", "pgup": "pageup", "pgdn": "pagedown", "prtsc": "printscreen"}
USAGES = {name: usage for usage, name in KEYS.items()}
MEDIA = {
    0xB5: "next", 0xB6: "previous", 0xB7: "stop", 0xCD: "play-pause",
    0xE2: "mute", 0xE9: "volume-up", 0xEA: "volume-down",
}
MEDIA_CODES = {name: code for code, name in MEDIA.items()}
SIMPLE_ACTIONS = {"none": 0x00, "dpi": 0x30, "scroll-up": 0x31, "scroll-down": 0x32, "tilt-left": 0x33, "tilt-right": 0x34}
ACTION_NAMES = {code: name for name, code in SIMPLE_ACTIONS.items()}

# What each record does out of the box, from the firmware's restore-defaults code.
DEFAULTS = (
    [bytes([b, 0, 0, 0, 0]) for b in (0x01, 0x02, 0x03, 0x33, 0x34, 0x30)]
    + [bytes([0x51, k, 0, 0, 0]) for k in (0x20, 0x23, 0x26, 0x2E, 0x1F, 0x22, 0x25, 0x2D, 0x1E, 0x21, 0x24, 0x27)]
    + [bytes([0x31, 0, 0, 0, 0]), bytes([0x32, 0, 0, 0, 0])]
)
# In record order, from GG's button list for this mouse.
RECORD_NAMES = [
    "left", "right", "middle", "tilt left", "tilt right", "dpi",
    "side 3", "side 6", "side 9", "side 12", "side 2", "side 5", "side 8", "side 11",
    "side 1", "side 4", "side 7", "side 10", "wheel up", "wheel down",
]


def record_index(name):
    """Find a button by name: `side 12`, `side-12`, `s12`, `tilt left`, `dpi`, `left`."""
    wanted = re.sub(r"[\s_-]+", " ", str(name).strip().lower())
    match = re.fullmatch(r"s(?:ide)? ?(\d+)", wanted)
    if match:
        wanted = f"side {int(match.group(1))}"
    if wanted in RECORD_NAMES:
        return RECORD_NAMES.index(wanted)
    raise AeroxError(f"No button called {name!r}. Buttons: {', '.join(RECORD_NAMES)}.")


def describe_action(record):
    """A record as the text parse_action takes, so what's printed can be typed back in."""
    kind = record[0]
    if 0x01 <= kind <= 0x08:
        return f"mouse{kind}"
    if kind == 0x51:
        keys = [KEYS.get(k, f"{k:#04x}") for k in record[1:] if k]
        return "+".join(keys) if keys else "none"
    if kind == 0x61:
        return "media:" + MEDIA.get(record[1], f"{record[1]:#04x}")
    return ACTION_NAMES.get(kind, "unknown " + bytes(record).hex(" "))


def parse_action(text):
    """Turn `alt+tab`, `mouse4`, `dpi`, `scroll-up`, `media:mute` or `none` into a 5-byte record."""
    t = text.strip().lower()
    if t == "nothing":
        t = "none"
    if t in SIMPLE_ACTIONS:
        return bytes([SIMPLE_ACTIONS[t], 0, 0, 0, 0])
    match = re.fullmatch(r"mouse ?([1-8])", t)
    if match:
        return bytes([int(match.group(1)), 0, 0, 0, 0])
    if t.startswith("media:"):
        name = t[6:]
        if name not in MEDIA_CODES:
            raise AeroxError(f"Unknown media action {name!r}. Choose from: {', '.join(MEDIA_CODES)}.")
        return bytes([0x61, MEDIA_CODES[name], 0, 0, 0])
    if t.startswith("keys:"):
        t = t[5:]
    names = [KEY_ALIASES.get(n.strip(), n.strip()) for n in re.split(r"\+(?!$)", t)]
    if not 1 <= len(names) <= 4:
        raise AeroxError("A button can send up to 4 keys at once.")
    unknown = [n for n in names if n not in USAGES]
    if unknown:
        raise AeroxError(f"Unknown key or action: {', '.join(unknown)}.")
    return bytes([0x51, *(USAGES[n] for n in names)]).ljust(5, b"\0")


def decode_buttons(records):
    out = []
    for i, name in enumerate(RECORD_NAMES):
        record = bytes(records[i * 5:i * 5 + 5])
        out.append({
            "record": i, "name": name, "raw": record.hex(),
            "action": describe_action(record), "default": record == DEFAULTS[i],
        })
    return out


class Aerox:
    """One open connection. Sends one command at a time and waits for its reply, because
    the dongle silently drops anything sent while a forwarded command is pending."""

    def __init__(self, node, kind, bcd, fd=None):
        self.node, self.kind, self.bcd = node, kind, bcd
        if bcd != FIRMWARE_BCD:
            raise AeroxError(
                f"Firmware {bcd} isn't 1.2.4 (bcd {FIRMWARE_BCD}), the only version this tool's "
                "commands were read from. Stopping rather than guess."
            )
        if fd is not None:
            self.fd = fd
            return
        try:
            self.fd = os.open(node, os.O_RDWR | os.O_NONBLOCK)
        except PermissionError:
            raise AeroxError(f"No permission for {node}. Run ./install.sh to add the udev rule.") from None
        # The other user is usually the GUI or another aerox, holding it for a moment.
        end = time.monotonic() + LOCK_TIMEOUT
        while True:
            try:
                fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return
            except BlockingIOError:
                if time.monotonic() >= end:
                    os.close(self.fd)
                    raise AeroxError(f"Another program is holding {node}. Close it and retry.") from None
                time.sleep(0.05)

    @classmethod
    def open(cls):
        return cls(*find_device())

    def close(self):
        os.close(self.fd)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def request(self, cmd, *args, timeout=REPLY_TIMEOUT):
        frame = build_frame(cmd, args)  # refuses before anything touches the device
        while select.select([self.fd], [], [], 0)[0]:
            os.read(self.fd, 64)  # whatever is queued isn't an answer to this
        os.write(self.fd, frame)
        end = time.monotonic() + timeout
        while (left := end - time.monotonic()) > 0:
            if not select.select([self.fd], [], [], left)[0]:
                break
            reply = os.read(self.fd, 64)
            if reply[:2] == UNREACHABLE:
                raise MouseUnreachable("The mouse didn't answer. It's off, asleep or out of range: move it and retry.")
            if reply_matches(cmd, reply):
                return reply
        raise MouseUnreachable(f"No reply to {cmd:#04x}. If the mouse is asleep, move it and retry.")

    def write(self, cmd, *args):
        """Send a write and check its status byte. 0 means the mouse took it."""
        reply = self.request(cmd, *args)
        if reply[1] != 0:
            raise AeroxError(f"The mouse refused command {cmd:#04x} (status {reply[1]}).")
        return reply

    def _confirm(self, what, read, want):
        """Settings apply a moment after the reply, so give the read-back a few tries."""
        for _ in range(5):
            got = read()
            if got == want:
                return
            time.sleep(0.05)
        raise AeroxError(f"Setting {what} didn't take: wanted {want}, read {got}.")

    def _wait_for_link(self, timeout=RESYNC_TIMEOUT):
        """Wait out a radio re-sync. A link check can't fail just because the link is down."""
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            try:
                if self.link_up():
                    return
            except MouseUnreachable:
                pass
            time.sleep(0.1)
        raise MouseUnreachable("The mouse didn't reconnect after the change. Move it, or turn it off and on.")

    def string(self, cmd, which=0):
        return self.request(cmd, which).split(b"\0")[0].decode("ascii", "replace")

    def link_up(self):
        """True when the mouse is reachable. Plugged in directly, it always is."""
        return self.kind == "wired" or self.request(0xBC)[1] == 1

    def check_mouse_firmware(self):
        version = self.string(0xD0)
        if version != FIRMWARE:
            raise AeroxError(f"Mouse firmware {version} isn't {FIRMWARE}, the only version this tool's commands were read from.")
        return version

    def ready(self):
        """Stop here unless the mouse is reachable and on the firmware these commands are from."""
        if not self.link_up():
            raise MouseUnreachable("The mouse isn't connected. It's off, asleep or out of range: move it and retry.")
        return self.check_mouse_firmware()

    def battery(self):
        return decode_battery(self.request(0xD2))

    def polling(self):
        return decode_polling(self.request(0xEB))

    def dpi(self):
        return decode_dpi(self.request(0xED))

    def sleep_ms(self):
        return int.from_bytes(self.request(0xE9)[1:5], "little")

    def low_power(self):
        return bool(self.request(0xE8)[1])

    def lighting(self):
        return decode_lighting(self.request(0xE3))

    def reactive(self):
        return decode_reactive(self.request(0xE6))

    def _button_records(self):
        return b"".join(self.request(0xEA, page)[1:n + 1] for page, n in ((0, 30), (1, 30), (2, 40)))

    def buttons(self):
        return decode_buttons(self._button_records())

    def set_polling(self, hz):
        index = {v: k for k, v in POLLING_HZ.items()}.get(hz)
        if index is None:
            raise AeroxError(f"Polling rate must be one of {sorted(POLLING_HZ.values())}, not {hz}.")
        if self.polling()["hz"] == hz:
            return
        self.write(0x6B, index)
        # The dongle re-syncs its radio to the new rate: the link drops for about 1.2 s (measured).
        time.sleep(0.2)
        self._wait_for_link()
        self._confirm("polling rate", lambda: self.polling()["hz"], hz)

    def set_dpi(self, dpis, current=0):
        """Set every stage. A stage's value is what the sensor runs at when it's selected."""
        if not 1 <= len(dpis) <= 5:
            raise AeroxError("Pick 1 to 5 DPI stages.")
        if not 0 <= current < len(dpis):
            raise AeroxError(f"Stage {current + 1} doesn't exist: there are {len(dpis)}.")
        levels = [dpi_to_level(d) for d in dpis]
        self.write(0x6D, len(levels), current, *levels, *([0] * (5 - len(levels))))
        self._confirm("DPI", lambda: (self.dpi()["levels"], self.dpi()["current"]), (levels, current))

    def set_stage(self, stage):
        count = self.dpi()["count"]
        if not 0 <= stage < count:
            raise AeroxError(f"Stage {stage + 1} doesn't exist: there are {count}.")
        self.write(0x6E, stage)
        self._confirm("DPI stage", lambda: self.dpi()["current"], stage)

    def set_sleep_ms(self, ms):
        if not _is_timer(ms):
            raise AeroxError("Sleep time must be off or up to 20 minutes, in half minutes.")
        self.write(0x69, *ms.to_bytes(4, "little"))
        self._confirm("sleep time", self.sleep_ms, ms)

    def set_low_power(self, on):
        self.write(0x68, int(bool(on)))
        self._confirm("low power mode", self.low_power, bool(on))

    def set_lighting(self, brightness=None, idle_brightness=None, smart=None, idle_timeout_ms=None):
        """Only the values given change: the command sets all four, so the rest are read first."""
        now = self.lighting()
        want = {
            "brightness": now["brightness"] if brightness is None else brightness,
            "idle_brightness": now["idle_brightness"] if idle_brightness is None else idle_brightness,
            "smart": now["smart"] if smart is None else bool(smart),
            "idle_timeout_ms": now["idle_timeout_ms"] if idle_timeout_ms is None else idle_timeout_ms,
        }
        if not (0 <= want["brightness"] <= 15 and 0 <= want["idle_brightness"] <= 15):
            raise AeroxError("Brightness runs from 0 to 15.")
        if not _is_timer(want["idle_timeout_ms"]):
            raise AeroxError("The idle timeout must be off or up to 20 minutes, in half minutes.")
        self.write(0x63, want["brightness"], want["idle_brightness"], int(want["smart"]), 0,
                   *want["idle_timeout_ms"].to_bytes(4, "little"))
        self._confirm("lighting", self.lighting, want)

    def set_reactive(self, enabled=None, random=None, color=None):
        now = self.reactive()
        want = {
            "enabled": now["enabled"] if enabled is None else bool(enabled),
            "random": now["random"] if random is None else bool(random),
            "color": now["color"] if color is None else parse_color(color).hex(),
        }
        self.write(0x66, int(want["enabled"]), int(want["random"]), *bytes.fromhex(want["color"]))
        self._confirm("reactive lighting", self.reactive, want)

    def set_zone_colors(self, colors):
        """Temporary colours: {zone 0-2: RRGGBB}. Not part of what `save` stores."""
        if not 1 <= len(colors) <= 3:
            raise AeroxError("Pick 1 to 3 zones.")
        data = []
        for zone, color in colors.items():
            data += [zone, *parse_color(color)]
        self.write(0x61, len(colors), *data)

    def clear_zone_colors(self):
        self.write(0x62)

    def set_records(self, records):
        """Write full button records, one command per page that changed."""
        now = self._button_records()
        new = bytes(records)
        if len(new) != 100:
            raise AeroxError("Expected 100 bytes of button records.")
        spans = ((0, 0, 30), (1, 30, 60), (2, 60, 100))
        for page, start, end in spans:
            if now[start:end] != new[start:end]:
                self.write(0x6A, page, *new[start:end])
        self._confirm("buttons", self._button_records, new)

    def bind(self, button, action):
        index = record_index(button)
        record = parse_action(action) if isinstance(action, str) else bytes(action)
        records = bytearray(self._button_records())
        records[index * 5:index * 5 + 5] = record
        self.set_records(records)

    def save(self, what="all"):
        """Erase and rewrite flash. Only changed blocks are written: the firmware tracks that."""
        blocks = list(SAVE_BLOCKS.values()) if what == "all" else [SAVE_BLOCKS.get(what)]
        if None in blocks:
            raise AeroxError(f"Save 'config', 'lighting' or 'all', not {what!r}.")
        battery = self.battery()
        if battery["level"] <= LOW_BATTERY_LEVEL and not battery["charging"]:
            raise AeroxError("The battery is nearly flat. Charge it first: saving erases flash.")
        for block in blocks:
            self.write(SAVE, block)

    def backup(self):
        return {
            "tool": f"aerox {__version__}",
            "firmware": self.string(0xD0),
            "polling_hz": self.polling()["hz"],
            "dpi": self.dpi(),
            "sleep_ms": self.sleep_ms(),
            "low_power": self.low_power(),
            "lighting": self.lighting(),
            "reactive": self.reactive(),
            "button_records": self._button_records().hex(),
        }

    def restore(self, saved):
        """Put a backup back, in RAM. Run save afterwards to make it permanent."""
        dpi = saved["dpi"]
        if any(v is None for v in dpi["dpi"]):
            self.write(0x6D, dpi["count"], dpi["current"], *dpi["levels"], *([0] * (5 - dpi["count"])))
        else:
            self.set_dpi(dpi["dpi"], dpi["current"])
        self.set_polling(saved["polling_hz"])
        if self.sleep_ms() != saved["sleep_ms"]:
            self.set_sleep_ms(saved["sleep_ms"])
        self.set_low_power(saved["low_power"])
        self.set_lighting(**saved["lighting"])
        self.set_reactive(**saved["reactive"])
        self.set_records(bytes.fromhex(saved["button_records"]))


def parse_color(color):
    if isinstance(color, (bytes, bytearray)):
        value = bytes(color)
    else:
        text = str(color).strip().lstrip("#")
        if not re.fullmatch(r"[0-9a-fA-F]{6}", text):
            raise AeroxError(f"Colours look like ff5a00, not {color!r}.")
        value = bytes.fromhex(text)
    if len(value) != 3:
        raise AeroxError("A colour is 3 bytes.")
    return value


def format_timer(ms):
    if ms == 0:
        return "off"
    return f"{ms / 60000:g} min" if ms >= 60000 else f"{ms / 1000:g} s"


def format_battery(b):
    state = "charging" if b["charging"] else "not charging"
    return f"{b['percent']}% (level {b['level']} of 21), {state}"


def format_polling(p):
    return f"{p['hz']} Hz" if p["hz"] else f"unknown index {p['index']}"


def format_dpi(d):
    shown = [str(dpi) if dpi else f"{level:#04x}" for dpi, level in zip(d["dpi"], d["levels"])]
    stages = " ".join(f"[{s}]" if i == d["current"] else s for i, s in enumerate(shown))
    return f"stage {d['current'] + 1} of {d['count']}: {stages}"


def format_lighting(light):
    smart = "smart lighting on" if light["smart"] else "smart lighting off"
    return (f"brightness {light['brightness']} of 15, dims to {light['idle_brightness']} "
            f"after {format_timer(light['idle_timeout_ms'])}, {smart}")


def format_reactive(r):
    if not r["enabled"]:
        return "off"
    return "on, random colours" if r["random"] else f"on, #{r['color']}"


def format_low_power(on):
    return "on" if on else "off"


def collect_status(mouse):
    status = {"node": mouse.node, "connection": mouse.kind}
    if mouse.kind == "dongle":
        status["dongle_firmware"] = mouse.string(0x90)
    if not mouse.link_up():
        status["link"] = False
        return status
    status.update(
        link=True,
        mouse_firmware=mouse.check_mouse_firmware(),
        battery=mouse.battery(),
        polling=mouse.polling(),
        dpi=mouse.dpi(),
        sleep_ms=mouse.sleep_ms(),
        low_power=mouse.low_power(),
        lighting=mouse.lighting(),
        reactive=mouse.reactive(),
    )
    return status


def print_status(s):
    print(f"Aerox 9 Wireless, {'through the dongle' if s['connection'] == 'dongle' else 'plugged in'} ({s['node']})")
    if "dongle_firmware" in s:
        print(f"  dongle    {s['dongle_firmware']}")
    if not s["link"]:
        print("  mouse     not connected: it's off, asleep or out of range. Move it and retry.")
        return
    print(f"  mouse     {s['mouse_firmware']}")
    print(f"  battery   {format_battery(s['battery'])}")
    print(f"  polling   {format_polling(s['polling'])}")
    print(f"  dpi       {format_dpi(s['dpi'])}")
    print(f"  sleep     {format_timer(s['sleep_ms'])}")
    print(f"  low power {format_low_power(s['low_power'])}")
    print(f"  lighting  {format_lighting(s['lighting'])}")
    print(f"  reactive  {format_reactive(s['reactive'])}")


def print_buttons(buttons):
    for b in buttons:
        print(f"  {b['name']:<11} {b['action']}{'' if b['default'] else '  (changed from default)'}")


UNSAVED = "Applied until the mouse powers off. Run `aerox save` to keep it."


def _show(args, value, formatter):
    print(json.dumps(value)) if args.json else print(formatter(value))
    return 0


def cmd_battery(mouse, args):
    return _show(args, mouse.battery(), format_battery)


def cmd_dpi(mouse, args):
    if args.values:
        try:
            dpis = [int(v) for v in args.values.split(",")]
        except ValueError:
            raise AeroxError(f"DPI stages look like 400,800,1600, not {args.values!r}.") from None
        mouse.set_dpi(dpis, (args.stage or 1) - 1)
        print(UNSAVED, file=sys.stderr)
    elif args.stage:
        mouse.set_stage(args.stage - 1)
        print(UNSAVED, file=sys.stderr)
    return _show(args, mouse.dpi(), format_dpi)


def cmd_polling(mouse, args):
    if args.hz is not None:
        mouse.set_polling(args.hz)
        print(UNSAVED, file=sys.stderr)
    return _show(args, mouse.polling(), format_polling)


def cmd_sleep(mouse, args):
    if args.minutes is not None:
        mouse.set_sleep_ms(round(args.minutes * 60000))
        print(UNSAVED, file=sys.stderr)
    return _show(args, mouse.sleep_ms(), format_timer)


def cmd_low_power(mouse, args):
    if args.state:
        mouse.set_low_power(args.state == "on")
        print(UNSAVED, file=sys.stderr)
    return _show(args, mouse.low_power(), format_low_power)


def cmd_buttons(mouse, args):
    buttons = mouse.buttons()
    print(json.dumps(buttons)) if args.json else print_buttons(buttons)
    return 0


def cmd_bind(mouse, args):
    mouse.bind(args.button, args.action)
    print(UNSAVED, file=sys.stderr)
    bound = mouse.buttons()[record_index(args.button)]
    print(json.dumps(bound)) if args.json else print(f"  {bound['name']:<11} {bound['action']}")
    return 0


def cmd_light(mouse, args):
    wanted = {}
    if args.brightness is not None:
        wanted["brightness"] = args.brightness
    if args.idle_brightness is not None:
        wanted["idle_brightness"] = args.idle_brightness
    if args.idle_minutes is not None:
        wanted["idle_timeout_ms"] = round(args.idle_minutes * 60000)
    if args.smart:
        wanted["smart"] = args.smart == "on"
    if wanted:
        mouse.set_lighting(**wanted)
        print(UNSAVED, file=sys.stderr)
    return _show(args, mouse.lighting(), format_lighting)


def cmd_color(mouse, args):
    if args.zone == "clear":
        mouse.clear_zone_colors()
        print("Back to the normal lighting.")
        return 0
    if not args.color:
        raise AeroxError("Give a colour: aerox color 1 ff5a00")
    zones = range(3) if args.zone == "all" else [int(args.zone) - 1] if args.zone in ("1", "2", "3") else None
    if zones is None:
        raise AeroxError(f"Zones are 1, 2, 3, all or clear, not {args.zone!r}.")
    mouse.set_zone_colors({z: args.color for z in zones})
    print("Temporary: `aerox color clear` undoes it, and it can't be saved.", file=sys.stderr)
    return 0


def cmd_reactive(mouse, args):
    wanted = {}
    if args.state:
        wanted["enabled"] = args.state == "on"
    if args.random:
        wanted["random"] = args.random == "on"
    if args.color:
        wanted["color"] = args.color
    if wanted:
        mouse.set_reactive(**wanted)
        print(UNSAVED, file=sys.stderr)
    return _show(args, mouse.reactive(), format_reactive)


def cmd_save(mouse, args):
    if not args.yes:
        answer = input(f"Write {args.what} settings to the mouse's flash? [y/N] ")
        if answer.strip().lower() not in ("y", "yes"):
            print("Nothing saved.")
            return 1
    mouse.save(args.what)
    print("Saved to the mouse.")
    return 0


def cmd_backup(mouse, args):
    with open(args.file, "w") as f:
        json.dump(mouse.backup(), f, indent=1)
    print(f"Saved to {args.file}")
    return 0


def cmd_restore(mouse, args):
    with open(args.file) as f:
        mouse.restore(json.load(f))
    print(UNSAVED, file=sys.stderr)
    return 0


def cmd_info(mouse, args):
    info = {"node": mouse.node, "connection": mouse.kind, "bcd": mouse.bcd}
    if mouse.kind == "dongle":
        info.update(dongle_firmware=mouse.string(0x90), dongle_git=mouse.string(0x90, 1))
    if mouse.link_up():
        info.update(mouse_firmware=mouse.string(0xD0), mouse_git=mouse.string(0xD0, 1))
    if args.json:
        print(json.dumps(info))
    else:
        for key, value in info.items():
            print(f"  {key:<16} {value}")
    return 0


def cmd_status(mouse, args):
    status = collect_status(mouse)
    print(json.dumps(status)) if args.json else print_status(status)
    return 0


NEEDS_LINK_CHECK_ONLY = {cmd_status, cmd_info}


def run(args):
    with Aerox.open() as mouse:
        if args.handler not in NEEDS_LINK_CHECK_ONLY:
            mouse.ready()
        return args.handler(mouse, args)


def build_parser():
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--json", action="store_true", default=argparse.SUPPRESS, help="print JSON")
    parser = argparse.ArgumentParser(prog="aerox", description="Configure a SteelSeries Aerox 9 Wireless.", parents=[common])
    parser.add_argument("--version", action="version", version=f"aerox {__version__}")
    parser.set_defaults(handler=cmd_status)
    sub = parser.add_subparsers(metavar="command")

    def add(name, handler, help_text):
        p = sub.add_parser(name, help=help_text, parents=[common])
        p.set_defaults(handler=handler)
        return p

    add("status", cmd_status, "everything at a glance")
    add("battery", cmd_battery, "battery level")
    add("info", cmd_info, "device and firmware details")
    p = add("dpi", cmd_dpi, "show or set DPI stages")
    p.add_argument("values", nargs="?", help="stages, like 400,800,1600")
    p.add_argument("--stage", type=int, help="which stage to use, from 1")
    p = add("polling", cmd_polling, "show or set the polling rate")
    p.add_argument("hz", nargs="?", type=int, choices=sorted(POLLING_HZ.values()))
    p = add("sleep", cmd_sleep, "show or set sleep time")
    p.add_argument("minutes", nargs="?", type=float, help="0 (off) to 20, in half minutes")
    p = add("low-power", cmd_low_power, "show or set the sensor's low power mode")
    p.add_argument("state", nargs="?", choices=["on", "off"])
    add("buttons", cmd_buttons, "what each button does")
    p = add("bind", cmd_bind, "bind a button to an action")
    p.add_argument("button", help="left, right, middle, tilt left, tilt right, dpi, side 1 to side 12, wheel up, wheel down")
    p.add_argument("action", help="alt+tab, mouse4, dpi, scroll-up, tilt-left, media:mute, none")
    p = add("light", cmd_light, "brightness, idle dimming, smart lighting")
    p.add_argument("--brightness", type=int)
    p.add_argument("--idle-brightness", type=int)
    p.add_argument("--idle-minutes", type=float)
    p.add_argument("--smart", choices=["on", "off"])
    p = add("color", cmd_color, "temporary zone colours")
    p.add_argument("zone", help="1, 2, 3, all or clear")
    p.add_argument("color", nargs="?", help="like ff5a00")
    p = add("reactive", cmd_reactive, "reactive lighting")
    p.add_argument("state", nargs="?", choices=["on", "off"])
    p.add_argument("--color", help="like ffffff")
    p.add_argument("--random", choices=["on", "off"])
    p = add("save", cmd_save, "write settings to the mouse's flash")
    p.add_argument("what", nargs="?", default="all", choices=["config", "lighting", "all"])
    p.add_argument("--yes", action="store_true", help="don't ask first")
    p = add("backup", cmd_backup, "save the current settings to a file")
    p.add_argument("file")
    p = add("restore", cmd_restore, "put a backup back")
    p.add_argument("file")
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    args.json = getattr(args, "json", False)
    try:
        return run(args)
    except AeroxError as e:
        print(f"aerox: {e}", file=sys.stderr)
        return 1
    except OSError as e:
        print(f"aerox: lost the device, was it unplugged? ({e})", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
