#!/usr/bin/env python3
"""aerox: read a SteelSeries Aerox 9 Wireless from Linux, with no vendor software.

Battery, DPI stages, polling rate, sleep time and button bindings, over the mouse's
vendor HID interface. Read-only: it can only send the commands in ALLOWED, and it
stops on any firmware other than 1.2.4, the one those commands were read from.

    aerox              status
    aerox battery      just the battery
    aerox dpi          DPI stages
    aerox polling      polling rate
    aerox sleep        sleep time
    aerox buttons      what each button is bound to
    aerox info         device and firmware details
    aerox ... --json   the same as JSON, for scripts
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

__version__ = "0.1.0"

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

ANY = None  # takes no arguments

# Every command this tool can send, with the arguments it may carry. Each handler was read
# in the firmware and touches no flash (notes in ~/aerox-re/FINDINGS.md, kept out of this repo). Bit 7 set does NOT mean "read" on
# this device: 0x91/0xD1 look like reads but wipe settings. So this is a list of known bytes
# and must never become a range or a mask.
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
    0xEB: ANY,      # polling rate
    0xED: ANY,      # DPI stages
    0xE9: ANY,      # sleep time
    0xEA: (0, 1, 2),  # button records, by page
}
ALLOWED = {**DONGLE_READS, **MOUSE_READS}

NO_ECHO = {0x90, 0xD0}  # these reply with a bare string
UNREACHABLE = b"\x40\xff"  # the dongle couldn't forward: mouse off, asleep or out of range
REPLY_TIMEOUT = 4.5  # the dongle gives up on a forwarded command after 4 s
POLLING_HZ = {0: 1000, 1: 500, 2: 250, 3: 125}


class AeroxError(Exception):
    """Something the user should read, not a bug."""


class Refused(AeroxError):
    """A command that isn't on the allowlist. Never reaches the device."""


class MouseUnreachable(AeroxError):
    """The dongle is here but can't reach the mouse."""


def check_allowed(cmd, args=()):
    """Raise Refused unless this exact command and arguments are allowlisted."""
    if cmd not in ALLOWED:
        raise Refused(f"command {cmd:#04x} is not on the allowlist")
    allowed = ALLOWED[cmd]
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
        try:
            fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(self.fd)
            raise AeroxError(f"Another program is using {node}.") from None

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

    def battery(self):
        return decode_battery(self.request(0xD2))

    def polling(self):
        return decode_polling(self.request(0xEB))

    def dpi(self):
        return decode_dpi(self.request(0xED))

    def sleep_ms(self):
        return int.from_bytes(self.request(0xE9)[1:5], "little")

    def buttons(self):
        records = b"".join(self.request(0xEA, page)[1:n + 1] for page, n in ((0, 30), (1, 30), (2, 40)))
        return decode_buttons(records)


def decode_battery(reply):
    """Bit 7 = charging. The rest is a level from 1 to 21, in 5% steps."""
    level = reply[1] & 0x7F
    return {"level": level, "percent": max(0, min(100, (level - 1) * 5)), "charging": bool(reply[1] & 0x80)}


def decode_polling(reply):
    return {"index": reply[1], "hz": POLLING_HZ.get(reply[1])}


def decode_dpi(reply):
    """The values go straight to a sensor register. The firmware has no DPI table, so they stay raw."""
    count, current = reply[1], reply[2]
    return {"count": count, "current": current, "values": list(reply[3:3 + count])}


KEYS = {0x04 + i: chr(ord("a") + i) for i in range(26)}
KEYS.update({0x1E + i: str((i + 1) % 10) for i in range(10)})
KEYS.update({0x3A + i: f"F{i + 1}" for i in range(12)})
KEYS.update({0x68 + i: f"F{i + 13}" for i in range(12)})
KEYS.update(zip(range(0x28, 0x39), (
    "enter", "esc", "backspace", "tab", "space", "-", "=", "[", "]", "\\", "#", ";", "'", "`", ",", ".", "/",
)))
KEYS.update(zip(range(0x46, 0x53), (
    "printscreen", "scrolllock", "pause", "insert", "home", "pageup", "delete", "end", "pagedown",
    "right", "left", "down", "up",
)))
KEYS[0x39] = "capslock"
KEYS.update(zip(range(0xE0, 0xE8), (
    "ctrl", "shift", "alt", "meta", "rctrl", "rshift", "ralt", "rmeta",
)))
MEDIA = {
    0xB5: "next track", 0xB6: "previous track", 0xB7: "stop", 0xCD: "play/pause",
    0xE2: "mute", 0xE9: "volume up", 0xEA: "volume down",
}
ACTIONS = {
    0x00: "nothing", 0x30: "next DPI stage", 0x31: "scroll up", 0x32: "scroll down",
    0x33: "axis -1, repeats", 0x34: "axis +1, repeats",
}
# What each record does out of the box, from the firmware's restore-defaults code.
DEFAULTS = (
    [bytes([b, 0, 0, 0, 0]) for b in (0x01, 0x02, 0x03, 0x33, 0x34, 0x30)]
    + [bytes([0x51, k, 0, 0, 0]) for k in (0x20, 0x23, 0x26, 0x2E, 0x1F, 0x22, 0x25, 0x2D, 0x1E, 0x21, 0x24, 0x27)]
    + [bytes([0x31, 0, 0, 0, 0]), bytes([0x32, 0, 0, 0, 0])]
)
# Named after those defaults: side button N is taken to be the one that types key N.
RECORD_NAMES = [
    "left", "right", "middle", "record 3", "record 4", "dpi button",
    "side 3", "side 6", "side 9", "side 12", "side 2", "side 5", "side 8", "side 11",
    "side 1", "side 4", "side 7", "side 10", "wheel up", "wheel down",
]


def describe_action(record):
    kind = record[0]
    if 0x01 <= kind <= 0x08:
        return f"mouse button {kind}"
    if kind == 0x51:
        keys = [KEYS.get(k, f"key {k:#04x}") for k in record[1:] if k]
        return "keys " + "+".join(keys) if len(keys) > 1 else (f"key {keys[0]}" if keys else "nothing")
    if kind == 0x61:
        return "media " + MEDIA.get(record[1], f"{record[1]:#04x}")
    return ACTIONS.get(kind, "unknown " + record.hex(" "))


def decode_buttons(records):
    out = []
    for i, name in enumerate(RECORD_NAMES):
        record = records[i * 5:i * 5 + 5]
        out.append({
            "record": i, "name": name, "raw": record.hex(),
            "action": describe_action(record), "default": record == DEFAULTS[i],
        })
    return out


def format_sleep(ms):
    return f"{ms / 60000:g} min" if ms >= 60000 else f"{ms / 1000:g} s"


def format_battery(b):
    state = "charging" if b["charging"] else "not charging"
    return f"{b['percent']}% (level {b['level']} of 21), {state}"


def format_polling(p):
    return f"{p['hz']} Hz" if p["hz"] else f"unknown index {p['index']}"


def format_dpi(d):
    stages = " ".join(f"[{v:#04x}]" if i == d["current"] else f"{v:#04x}" for i, v in enumerate(d["values"]))
    return f"stage {d['current'] + 1} of {d['count']}: {stages}"


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
    print(f"  sleep     {format_sleep(s['sleep_ms'])}")


def print_buttons(buttons):
    for b in buttons:
        print(f"  {b['name']:<11} {b['action']}{'' if b['default'] else '  (changed from default)'}")


def run(args):
    node, kind, bcd = find_device()
    with Aerox(node, kind, bcd) as mouse:
        if args.command == "status":
            status = collect_status(mouse)
            print(json.dumps(status)) if args.json else print_status(status)
            return 0
        if args.command == "info":
            info = {"node": node, "connection": kind, "bcd": bcd}
            if kind == "dongle":
                info.update(dongle_firmware=mouse.string(0x90), dongle_git=mouse.string(0x90, 1))
            if mouse.link_up():
                info.update(mouse_firmware=mouse.string(0xD0), mouse_git=mouse.string(0xD0, 1))
            if args.json:
                print(json.dumps(info))
            else:
                for key, value in info.items():
                    print(f"  {key:<16} {value}")
            return 0

        if not mouse.link_up():
            raise MouseUnreachable("The mouse isn't connected. It's off, asleep or out of range: move it and retry.")
        mouse.check_mouse_firmware()
        reader, formatter = {
            "battery": (mouse.battery, format_battery),
            "polling": (mouse.polling, format_polling),
            "dpi": (mouse.dpi, format_dpi),
            "sleep": (mouse.sleep_ms, format_sleep),
            "buttons": (mouse.buttons, None),
        }[args.command]
        value = reader()
        if args.json:
            print(json.dumps(value))
        elif formatter:
            print(formatter(value))
        else:
            print_buttons(value)
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(prog="aerox", description="Read a SteelSeries Aerox 9 Wireless. Read-only.")
    parser.add_argument("command", nargs="?", default="status",
                        choices=["status", "battery", "dpi", "polling", "sleep", "buttons", "info"])
    parser.add_argument("--json", action="store_true", help="print JSON")
    parser.add_argument("--version", action="version", version=f"aerox {__version__}")
    args = parser.parse_args(argv)
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
