#!/usr/bin/env python3
"""arctis: configure a SteelSeries Arctis 9 Wireless from Linux, with no vendor software.

    arctis                     status: the headset's link, battery, ChatMix, mic and sidetone
    arctis sidetone [LEVEL]    hear yourself: off, low, medium or high
    arctis mic [DB]            microphone gain, -25 to 0 dB
    arctis surround [on|off]   the transmitter's own surround
    arctis eq [PRESET|DB...]   flat, bass, reference, smiley, or ten bands from -10 to 10 dB
    arctis sleep [MIN]         turn the headset off after this many minutes without sound, 0 for never
    arctis save                write the current settings to the transmitter's flash
    arctis info                firmware versions
    arctis ... --json          the same as JSON, for scripts

Talks to the transmitter's vendor HID interface. Stops on any transmitter firmware but 1.1, and can only send the
commands below. Changes apply right away and last until the transmitter loses power, unless you run `arctis save`.
"""
import argparse
import fcntl
import glob
import json
import os
import select
import struct
import sys
import time

__version__ = "1.0.0"

VENDOR, PRODUCT = "00001038", "000012C2"
FIRMWARE_BCD = "0101"  # bcdDevice of transmitter firmware 1.1, the only version these commands were read from
# The vendor interface: 64 bytes in, 64 out, 514 feature, no report IDs. The other two interfaces carry media keys.
DESCRIPTOR = bytes.fromhex("06c0ff0901a10106c1ff150026ff00750809f09540810209f19540910209f2960202b102c0")
SYSFS = "/sys/class/hidraw"
COMMAND_GAP = 0.02  # between commands, as the vendor's own software leaves
REPLY_TIMEOUT = 0.35  # replies come within a few ms
READ_TRIES = 3

# Every command this can send. Each handler was read in the transmitter's firmware: the reads copy memory into the reply,
# and the writes store a setting in memory and pass it on to the headset. Only SAVE reaches flash.
READS = {
    0x10: "firmware versions",
    0x13: "the ten EQ bands",
    0x14: "the sleep timer",
    0x15: "microphone gain",
    0x16: "sidetone",
    0x17: "surround",
    0x1B: "EQ preset",
    0x20: "the headset's link, battery, ChatMix, mic and sidetone",
    0x23: "a fixed reply, to check it's listening",
}
SAVE = 0x09


def _int8(value):
    return value - 256 if value > 127 else value


SIDETONE = {"off": -64, "low": -18, "medium": -8, "high": -3}  # the four levels the vendor's own software offers
SIDETONE_NAMES = {db: name for name, db in SIDETONE.items()}
MIC_DB = range(-25, 1)
EQ_PRESETS = {"flat": 0, "bass": 1, "reference": 2, "smiley": 3, "custom": 4}
EQ_PRESET_NAMES = {n: name for name, n in EQ_PRESETS.items()}
EQ_BANDS = ["31", "62", "125", "250", "500", "1K", "2K", "4K", "8K", "16K"]  # Hz
EQ_UNIT = 1 << 23  # one dB
EQ_LIMIT = 10 * EQ_UNIT
SLEEP_MAX_MIN = 90


def _ten_bands(args):
    return len(args) == 40 and all(abs(v) <= EQ_LIMIT for v in struct.unpack("<10i", bytes(args)))


# Writes, each with the only arguments it may carry. The firmware ignores out-of-range surround and preset values, but
# stores any byte for the others, so these limits are the vendor software's own ranges.
WRITES = {
    0x03: ("EQ bands", _ten_bands),
    0x04: ("sleep timer", lambda a: len(a) == 2 and (a[0] << 8 | a[1]) <= SLEEP_MAX_MIN * 60),
    0x05: ("microphone gain", lambda a: len(a) == 1 and _int8(a[0]) in MIC_DB),
    0x06: ("sidetone", lambda a: len(a) == 1 and _int8(a[0]) in SIDETONE_NAMES),
    0x07: ("surround", lambda a: len(a) == 1 and a[0] in (0, 1)),
    0x0B: ("EQ preset", lambda a: len(a) == 1 and a[0] in EQ_PRESET_NAMES),
    SAVE: ("save to flash", lambda a: len(a) == 0),
}
# Never sent, from reading the firmware: 01 reboots, and 01 00 01 also resets everything and saves it. 02 switches to firmware
# update modes. 0C and 25 write device data. FF is for the factory. 08, 0A, 0E, 1A and 24 weren't traced.


class ArctisError(Exception):
    pass


class Refused(ArctisError):
    """A command or argument that isn't allowed. Never reaches the device."""


class NotConnected(ArctisError):
    """The transmitter is here, but the headset isn't on or in range."""


def check_allowed(cmd, args=()):
    args = tuple(args)
    if cmd in READS:
        if args:
            raise Refused(f"command {cmd:#04x} takes no arguments")
    elif cmd in WRITES:
        if not WRITES[cmd][1](args):
            raise Refused(f"command {cmd:#04x} ({WRITES[cmd][0]}) doesn't accept {list(args)}")
    else:
        raise Refused(f"command {cmd:#04x} is not on the allowlist")


def build_frame(cmd, args=()):
    """Report number 0, the command, the selector the firmware requires to be 0, then the arguments, zero padded to 64."""
    check_allowed(cmd, args)
    return bytes([0x00, cmd, 0x00, *args]).ljust(65, b"\0")


def _zeros(data):
    return not any(data)


# Replies don't repeat the command, and the transmitter also sends status reports unasked. Every reply starts from a
# cleared buffer, so a short answer is followed by zeros, which a status report never is.
VALID = {
    0x10: lambda r: r[:2] == b"\x01\x01",
    0x13: lambda r: _zeros(r[40:]) and all(abs(v) <= EQ_LIMIT for v in struct.unpack("<10i", r[:40])),
    0x14: lambda r: _zeros(r[2:]),
    0x15: lambda r: _zeros(r[1:]) and _int8(r[0]) in range(-64, 64),
    0x16: lambda r: _zeros(r[1:]),
    0x17: lambda r: _zeros(r[1:]) and r[0] in (0, 1),
    0x1B: lambda r: _zeros(r[1:]) and r[0] in EQ_PRESET_NAMES,
    0x20: lambda r: r[0] in (0xAA, 0x55),
    0x23: lambda r: r[:4] == b"\xaa\x55\x23\xdc" and _zeros(r[4:]),
}


def find_device(sysfs=SYSFS):
    """Return (node, bcd) for the vendor interface of a connected Arctis 9 transmitter."""
    for hidraw in sorted(glob.glob(f"{sysfs}/hidraw*")):
        try:
            with open(f"{hidraw}/device/uevent") as f:
                uevent = f.read().upper()
            with open(f"{hidraw}/device/report_descriptor", "rb") as f:
                descriptor = f.read()
        except OSError:
            continue
        if f":{VENDOR}:{PRODUCT}" not in uevent or descriptor != DESCRIPTOR:
            continue
        path = os.path.realpath(f"{hidraw}/device")
        while path != "/" and not os.path.exists(f"{path}/bcdDevice"):
            path = os.path.dirname(path)
        with open(f"{path}/bcdDevice") as f:
            bcd = f.read().strip()
        return "/dev/" + os.path.basename(hidraw), bcd
    raise ArctisError("No Arctis 9 transmitter found. Is it plugged in?")


def decode_status(reply):
    """The status reply. Battery voltage is in millivolts, as the transmitter reports it."""
    if reply[0] != 0xAA:
        return {"ready": False}
    return {
        "ready": True, "connected": reply[1] == 1, "rf_status": reply[1],
        "battery_mv": reply[2] << 8 | reply[3], "battery_status": reply[4],
        "mic_muted": bool(reply[5]), "mic_db": _int8(reply[6]),
        "sidetone_on": bool(reply[7]), "sidetone_db": _int8(reply[8]),
        "chat": reply[9], "game": reply[10], "mode": reply[11],
    }


def decode_versions(reply):
    return {
        "transmitter": f"{reply[1]}.{reply[0]}",
        "conexant": ".".join(str(b) for b in reply[2:6]),
        "codec": f"{reply[6]}.{reply[7]}",
        "headset": f"{reply[9]}.{reply[8]}",
        "radio_transmitter": bytes(reply[10:14]).decode("ascii", "replace").strip("\0"),
        "radio_headset": bytes(reply[14:18]).decode("ascii", "replace").strip("\0"),
        "bluetooth": f"{reply[18]}.{reply[19]}",
    }


class Arctis:
    """One open connection. One command at a time, with the vendor software's gap between them."""

    def __init__(self, node, bcd, fd=None):
        self.node, self.bcd = node, bcd
        if bcd != FIRMWARE_BCD:
            raise ArctisError(f"Transmitter firmware {bcd} isn't 1.1 (bcd {FIRMWARE_BCD}), the only version this tool's "
                              "commands were read from. Stopping rather than guess.")
        if fd is not None:
            self.fd = fd
            return
        try:
            self.fd = os.open(node, os.O_RDWR | os.O_NONBLOCK)
        except PermissionError:
            raise ArctisError(f"No permission for {node}. Run ./install.sh to add the udev rule.") from None
        try:
            fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(self.fd)
            raise ArctisError(f"Another program is holding {node}. Close it and retry.") from None

    @classmethod
    def open(cls):
        return cls(*find_device())

    def close(self):
        os.close(self.fd)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def _send(self, cmd, args=()):
        frame = build_frame(cmd, args)  # refuses before anything touches the device
        while select.select([self.fd], [], [], 0)[0]:
            os.read(self.fd, 64)  # whatever is queued isn't an answer to this
        os.write(self.fd, frame)

    def read(self, cmd):
        """Send a read and return the first reply that has its shape. A reply can go missing while the transmitter is busy
        with one of its own reports, and a read changes nothing, so it's asked again."""
        if cmd not in READS:
            raise Refused(f"command {cmd:#04x} is not a read")
        try:
            for _ in range(READ_TRIES):
                self._send(cmd)
                end = time.monotonic() + REPLY_TIMEOUT
                while True:
                    left = end - time.monotonic()
                    if left <= 0 or not select.select([self.fd], [], [], left)[0]:
                        break
                    reply = os.read(self.fd, 64)
                    if len(reply) >= 64 and VALID[cmd](reply):
                        return reply
                time.sleep(COMMAND_GAP)
            raise ArctisError(f"No reply to {cmd:#04x}. Replug the transmitter and retry.")
        finally:
            time.sleep(COMMAND_GAP)

    def write(self, cmd, *args):
        """Writes get no reply. Each setter reads its value back."""
        self._send(cmd, args)
        time.sleep(COMMAND_GAP)

    def ready(self):
        if self.read(0x23)[:4] != b"\xaa\x55\x23\xdc":
            raise ArctisError("The transmitter answered oddly. Replug it and retry.")
        versions = decode_versions(self.read(0x10))
        if versions["transmitter"] != "1.1":
            raise ArctisError(f"Transmitter firmware {versions['transmitter']} isn't 1.1. Stopping rather than guess.")

    def status(self):
        return decode_status(self.read(0x20))

    def versions(self):
        return decode_versions(self.read(0x10))

    def sidetone(self):
        return _int8(self.read(0x16)[0])

    def mic(self):
        return _int8(self.read(0x15)[0])

    def surround(self):
        return bool(self.read(0x17)[0])

    def eq_preset(self):
        return self.read(0x1B)[0]

    def eq_bands(self):
        return [v / EQ_UNIT for v in struct.unpack("<10i", self.read(0x13)[:40])]

    def sleep_seconds(self):
        reply = self.read(0x14)
        return reply[0] << 8 | reply[1]

    def _confirm(self, what, read, want):
        for _ in range(5):
            got = read()
            if got == want:
                return
            time.sleep(0.05)
        raise ArctisError(f"Setting {what} didn't take: wanted {want}, read {got}.")

    def set_sidetone(self, db):
        self.write(0x06, db & 0xFF)
        self._confirm("sidetone", self.sidetone, db)

    def set_mic(self, db):
        self.write(0x05, db & 0xFF)
        self._confirm("microphone gain", self.mic, db)

    def set_surround(self, on):
        self.write(0x07, int(on))
        self._confirm("surround", self.surround, bool(on))

    def set_eq_preset(self, preset):
        self.write(0x0B, preset)
        self._confirm("EQ preset", self.eq_preset, preset)

    def set_eq_bands(self, dbs):
        values = [round(db * EQ_UNIT) for db in dbs]
        self.write(0x03, *struct.pack("<10i", *values))
        self._confirm("EQ bands", lambda: [round(v * EQ_UNIT) for v in self.eq_bands()], values)

    def set_sleep(self, seconds):
        self.write(0x04, seconds >> 8, seconds & 0xFF)
        self._confirm("sleep timer", self.sleep_seconds, seconds)

    def save(self):
        self.write(SAVE)


def describe_status(s):
    if not s["ready"]:
        return "The transmitter is starting up. Try again in a moment."
    lines = []
    if not s["connected"]:
        lines.append("Headset: not connected. Turn it on, or bring it closer.")
    else:
        lines.append(f"Headset: connected, battery {s['battery_mv'] / 1000:.2f} V")
    lines.append(f"ChatMix: chat {s['chat']}, game {s['game']}")
    lines.append(f"Mic: {'muted' if s['mic_muted'] else 'on'}, gain {s['mic_db']} dB")
    sidetone = SIDETONE_NAMES.get(s["sidetone_db"], f"{s['sidetone_db']} dB")
    lines.append(f"Sidetone: {sidetone}" if s["sidetone_on"] else "Sidetone: off")
    return "\n".join(lines)


def parse_args(argv):
    p = argparse.ArgumentParser(prog="arctis", description="Configure an Arctis 9 Wireless with no vendor software.")
    p.add_argument("--version", action="version", version=__version__)
    p.add_argument("--json", action="store_true", help="print JSON")
    sub = p.add_subparsers(dest="command")
    sub.add_parser("status", help="the headset's link, battery, ChatMix, mic and sidetone (the default)")
    sub.add_parser("info", help="firmware versions")
    s = sub.add_parser("sidetone", help="hear yourself in the headset")
    s.add_argument("level", nargs="?", choices=list(SIDETONE))
    m = sub.add_parser("mic", help="microphone gain")
    m.add_argument("db", nargs="?", type=int, help="-25 to 0")
    r = sub.add_parser("surround", help="the transmitter's own surround")
    r.add_argument("state", nargs="?", choices=("on", "off"))
    e = sub.add_parser("eq", help="EQ: a preset, or ten bands from -10 to 10 dB")
    e.add_argument("values", nargs="*", help="flat, bass, reference, smiley, or ten numbers for " + " ".join(EQ_BANDS) + " Hz")
    z = sub.add_parser("sleep", help="turn the headset off after this many minutes without sound")
    z.add_argument("minutes", nargs="?", type=int, help=f"0 for never, up to {SLEEP_MAX_MIN}")
    v = sub.add_parser("save", help="write the current settings to the transmitter's flash")
    v.add_argument("--yes", action="store_true", help="don't ask first")
    return p.parse_args(argv)


def run(args, device):
    cmd = args.command or "status"
    out, changed = {}, False
    device.ready()
    if cmd == "status":
        out = device.status()
        text = describe_status(out)
    elif cmd == "info":
        out = device.versions()
        text = "\n".join(f"{k.replace('_', ' ')}: {v}" for k, v in out.items())
    elif cmd == "sidetone":
        if args.level:
            device.set_sidetone(SIDETONE[args.level])
            changed = True
        db = device.sidetone()
        out = {"sidetone_db": db, "level": SIDETONE_NAMES.get(db)}
        text = f"Sidetone: {SIDETONE_NAMES.get(db, f'{db} dB')}"
    elif cmd == "mic":
        if args.db is not None:
            if args.db not in MIC_DB:
                raise ArctisError("Microphone gain goes from -25 to 0 dB.")
            device.set_mic(args.db)
            changed = True
        out = {"mic_db": device.mic()}
        text = f"Microphone gain: {out['mic_db']} dB"
    elif cmd == "surround":
        if args.state:
            device.set_surround(args.state == "on")
            changed = True
        out = {"surround": device.surround()}
        text = f"Surround: {'on' if out['surround'] else 'off'}"
    elif cmd == "eq":
        if len(args.values) == 1:
            name = args.values[0].lower()
            if name not in EQ_PRESETS or name == "custom":
                raise ArctisError(f"Presets: {', '.join(n for n in EQ_PRESETS if n != 'custom')}. Or give ten numbers.")
            device.set_eq_preset(EQ_PRESETS[name])
            changed = True
        elif args.values:
            try:
                dbs = [float(v) for v in args.values]
            except ValueError:
                raise ArctisError("EQ bands are numbers, in dB.") from None
            if len(dbs) != 10 or any(abs(db) > 10 for db in dbs):
                raise ArctisError(f"Give ten bands from -10 to 10 dB, for {' '.join(EQ_BANDS)} Hz.")
            device.set_eq_bands(dbs)
            device.set_eq_preset(EQ_PRESETS["custom"])
            changed = True
        preset = device.eq_preset()
        out = {"preset": EQ_PRESET_NAMES.get(preset, preset), "bands_db": device.eq_bands()}
        text = f"EQ: {out['preset']}\n" + "  ".join(f"{hz} {db:+.1f}" for hz, db in zip(EQ_BANDS, out["bands_db"]))
    elif cmd == "sleep":
        if args.minutes is not None:
            if not 0 <= args.minutes <= SLEEP_MAX_MIN:
                raise ArctisError(f"Sleep is 0 (never) to {SLEEP_MAX_MIN} minutes.")
            device.set_sleep(args.minutes * 60)
            changed = True
        seconds = device.sleep_seconds()
        out = {"sleep_seconds": seconds}
        text = "Sleep: never" if seconds == 0 else f"Sleep: after {seconds // 60} min without sound" + (f" {seconds % 60} s" if seconds % 60 else "")
    elif cmd == "save":
        if not args.yes and input("Write the current settings to the transmitter's flash? Type yes: ").strip() != "yes":
            print("Nothing written.")
            return 1
        device.save()
        out = {"saved": True}
        text = "Saved. They'll come back after the transmitter loses power."
    if args.json:
        print(json.dumps(out))
    else:
        print(text)
        if changed:
            print("This lasts until the transmitter loses power. arctis save keeps it.")
    return 0


def main(argv=None):
    args = parse_args(sys.argv[1:] if argv is None else argv)
    try:
        with Arctis.open() as device:
            return run(args, device)
    except ArctisError as e:
        print(f"arctis: {e}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
