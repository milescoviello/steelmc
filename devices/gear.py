"""Talk to SteelSeries devices from definitions: which commands set what, and how each value is turned into bytes.

The definitions are JSON files in families/. Only commands listed in one are ever sent, every value is checked before anything
is, and most devices can't report their settings back, so the last values set here are kept in ~/.config/steelmc.
"""
import fcntl
import glob
import json
import os
import re
import select
import time

FAMILIES = os.path.join(os.path.dirname(os.path.realpath(__file__)), "families")
HIDRAW = "/sys/class/hidraw"
COMMAND_GAP = 0.05
REPLY_TIMEOUT = 1.0


class GearError(Exception):
    pass


class Refused(GearError):
    """A value that doesn't fit the setting. Nothing was sent."""


def load_families(path=FAMILIES):
    """{product id: (family, product)} for every product a definition covers."""
    out = {}
    for name in sorted(glob.glob(os.path.join(path, "*.json"))):
        with open(name) as f:
            for family in json.load(f)["families"]:
                for pid, product in family["products"].items():
                    out[pid] = (family, product)
    return out


# ---- encoding values -------------------------------------------------------------------------------------------------

COLORS = {"red": "ff0000", "green": "00ff00", "lime": "00ff00", "blue": "0000ff", "white": "ffffff", "black": "000000", "off": "000000",
          "yellow": "ffff00", "cyan": "00ffff", "aqua": "00ffff", "magenta": "ff00ff", "fuchsia": "ff00ff", "orange": "ff8000",
          "purple": "800080", "pink": "ff69b4"}


def parse_color(text):
    t = str(text).strip().lower().lstrip("#")
    t = COLORS.get(t, t)
    if len(t) == 3 and re.fullmatch(r"[0-9a-f]{3}", t):
        t = "".join(c * 2 for c in t)
    if not re.fullmatch(r"[0-9a-f]{6}", t):
        raise Refused(f"{text!r} isn't a colour. Use RRGGBB, like ff8000, or a name like red.")
    return [int(t[i:i + 2], 16) for i in (0, 2, 4)]


def le(value, length):
    return [(value >> (8 * i)) & 0xFF for i in range(length)]


def snap(value, first, last, step):
    if not first <= value <= last:
        raise Refused(f"{value} is outside {first} to {last}.")
    return first + round((value - first) / step) * step


def nearest(choices, value):
    return min(choices, key=lambda c: abs(c - value))


def _number(text):
    try:
        return int(float(str(text).strip()))
    except ValueError:
        raise Refused(f"{text!r} isn't a number.") from None


def encode_choice(s, value):
    key = str(value).strip().lower()
    for k, v in s["choices"].items():
        if str(k).lower() == key:
            return v if isinstance(v, list) else [v]
    raise Refused(f"{value!r} isn't one of: {', '.join(str(k) for k in s['choices'])}.")


def encode_number(s, value):
    first, last, step = s["input"]
    out_first, _, out_step = s["output"]
    matched = snap(_number(value), first, last, step)
    return le(out_first + (matched - first) // step * out_step, s.get("bytes", 1))


def encode_number_choice(s, value):
    first, last, _ = s["input"]
    table = {int(k): v for k, v in s["table"].items()}
    v = _number(value)
    if not first <= v <= last:
        raise Refused(f"{v} is outside {first} to {last}.")
    return le(table[nearest(table, v)], s.get("bytes", 1))


def _dpi_list(value):
    items = value if isinstance(value, (list, tuple)) else str(value).replace(" ", "").split(",")
    dpis = [_number(d) for d in items if str(d) != ""]
    if not dpis:
        raise Refused("Give at least one DPI, like 800 or 400,800,1600.")
    return dpis


def encode_dpi_stages(s, value):
    dpis = _dpi_list(value)
    if len(dpis) > s["max_stages"]:
        raise Refused(f"Up to {s['max_stages']} DPI stages, not {len(dpis)}.")
    first, last, step = s["input"]
    if "table" in s:
        table = {int(k): v for k, v in s["table"].items()}
        values = []
        for d in dpis:
            if not first <= d <= last:
                raise Refused(f"{d} DPI is outside {first} to {last}.")
            values += le(table[nearest(table, d)], s["bytes"])
    else:
        out_first, _, out_step = s["output"]
        values = []
        for d in dpis:
            matched = snap(d, first, last, step)
            values += le(out_first + (matched - first) // step * out_step, s["bytes"])
    count = len(dpis) if s.get("count", "number") == "number" else 0xFF >> (8 - len(dpis))
    return [count, s["first_stage"]] + values


def encode_color(s, value):
    return parse_color(value)


def encode_reactive_color(s, value):
    if str(value).strip().lower() in ("off", "none", "disable"):
        return [0, 0, 0, 0, 0]
    return [1, 0] + parse_color(value)


def encode_action(s, value):
    return []


# Keys a button can send, by name, as HID usages; media actions as consumer usages.
KEYS = {chr(ord("a") + i): 0x04 + i for i in range(26)}
KEYS.update({str((i + 1) % 10): 0x1E + i for i in range(10)})
KEYS.update({f"f{i + 1}": 0x3A + i for i in range(12)})
KEYS.update({f"f{i + 13}": 0x68 + i for i in range(12)})
KEYS.update(dict(zip(("enter", "escape", "backspace", "tab", "space", "minus", "equal", "leftbracket", "rightbracket", "backslash",
                      "nonushash", "semicolon", "quote", "backtick", "comma", "dot", "slash", "capslock"), range(0x28, 0x3A))))
KEYS.update(dict(zip(("printscreen", "scrolllock", "pause", "insert", "home", "pageup", "delete", "end", "pagedown", "right", "left",
                      "down", "up"), range(0x46, 0x53))))
KEYS.update(dict(zip(("leftctrl", "leftshift", "leftalt", "super", "rightctrl", "rightshift", "rightalt", "rightsuper"), range(0xE0, 0xE8))))
KEYS.update({"esc": 0x29, "ctrl": 0xE0, "shift": 0xE1, "alt": 0xE2, "meta": 0xE3, "win": 0xE3})
KEYS.update(dict(zip(("numlock", "kpslash", "kpasterisk", "kpminus", "kpplus", "kpenter"), range(0x53, 0x59))))
KEYS.update({f"kp{i + 1}": 0x59 + i for i in range(9)})
KEYS.update({"kp0": 0x62, "kpdot": 0x63, "nonusbackslash": 0x64, "menu": 0x65})
MEDIA = {"mute": 0xE2, "playpause": 0xCD, "next": 0xB5, "previous": 0xB6, "stop": 0xB7, "volumeup": 0xE9, "volumedown": 0xEA}


def parse_key(text):
    """A key by name (w, f1, leftshift, kp5) or by HID usage number (0x1a or 26)."""
    t = str(text).strip().lower()
    if t in KEYS:
        return KEYS[t]
    try:
        return int(t, 0)
    except ValueError:
        raise Refused(f"{text!r} isn't a key name. Use one like w, f1, space, leftshift, or a HID number like 0x1a.") from None


def _key_value(s, text):
    """The bytes for one key's value: a colour, or one of the setting's named values (numbers match 1.5 and 1.50 alike)."""
    if s.get("value") == "color":
        return parse_color(text)
    values = s["values"]
    t = str(text).strip().lower()
    if t not in values:
        try:
            t = f"{float(t):.1f}"
        except ValueError:
            pass
    if t not in values:
        names = list(values)
        shown = f"{names[0]} to {names[-1]}" if len(names) > 8 else ", ".join(names)
        raise Refused(f"{text!r} isn't a value here. Use {shown}" + (f" {s['unit']}" if s.get("unit") else "") + ".")
    return list(values[t])


def encode_per_key(s, value):
    """One value for every key, then KEY=VALUE for any key that should differ: 1.5 or 1.5,w=0.5,a=0.5.
    Keys left out get the setting's default. Sends the key count, then each key's HID usage and its value."""
    keys = s["keys"]
    every, apart = None, {}
    for part in (p.strip() for p in str(value).split(",")):
        if not part:
            continue
        if "=" in part:
            name, v = part.split("=", 1)
            hid = parse_key(name)
            if hid not in keys:
                raise Refused(f"{name.strip()} isn't one of the keys this setting covers.")
            apart[hid] = _key_value(s, v)
        elif every is None:
            every = _key_value(s, part)
        else:
            raise Refused("Give one value for every key first, then KEY=VALUE for the keys that differ.")
    if every is None:
        if s.get("default") is None:
            raise Refused("Give a value for every key first, like 1.5 or 1.5,w=0.5.")
        every = _key_value(s, s["default"])
    out = ([len(keys)] if s.get("count", True) else []) + list(s.get("after_count", []))
    for hid in keys:
        out += [hid] + apart.get(hid, every)
    return out


def encode_zones(s, value):
    """One colour for every zone, or one per zone in order, comma separated."""
    parts = [p.strip() for p in str(value).split(",") if p.strip()]
    if len(parts) == 1:
        parts = parts * s["zones"]
    if len(parts) != s["zones"]:
        raise Refused(f"Give one colour, or {s['zones']} colours (one per zone).")
    out = []
    for p in parts:
        out += parse_color(p) + list(s.get("zone_suffix", []))
    return out


def encode_buttons(s, value):
    """NAME=ACTION,NAME=ACTION, or default. An action is another button, disable, dpi, scrollup, scrolldown, a key, or a media key."""
    buttons = {k.lower(): dict(v) for k, v in s["buttons"].items()}
    wanted = {}
    text = str(value).strip()
    if text.lower() != "default":
        for part in text.split(","):
            if "=" not in part:
                raise Refused(f"{part!r} should look like button2=disable.")
            name, action = (x.strip().lower() for x in part.split("=", 1))
            if name not in buttons:
                raise Refused(f"No button called {name!r}. Buttons: {', '.join(buttons)}.")
            wanted[name] = action
    special = {k: v for k, v in s.get("special", {}).items() if v is not None}
    if "disable" in special:
        special["disabled"] = special["disable"]
    packet = [0] * (len(buttons) * s["field_length"])
    for name, data in buttons.items():
        action = wanted.get(name, data["default"]).lower()
        if action == "default":
            action = data["default"].lower()
        offset = data["offset"]
        if action in buttons:
            packet[offset] = buttons[action]["id"]
        elif action in special:
            packet[offset] = special[action]
        elif action in MEDIA and s.get("media") is not None:
            packet[offset], packet[offset + 1] = s["media"], MEDIA[action]
        elif action in KEYS and s.get("keyboard") is not None:
            packet[offset], packet[offset + 1] = s["keyboard"], KEYS[action]
        else:
            raise Refused(f"{action!r} isn't something {name} can do here.")
    return packet


def parse_gradient(value):
    """[ms:]COLOR@PERCENT,COLOR@PERCENT... or one colour. Returns (ms, [(percent, rgb)])."""
    text = str(value).strip()
    duration = 1000
    match = re.match(r"^(\d+)\s*ms\s*:\s*(.*)$", text)
    if match:
        duration, text = int(match.group(1)), match.group(2)
    stops = []
    for part in text.split(","):
        part = part.strip()
        if "@" in part:
            color, pos = part.rsplit("@", 1)
            stops.append((_number(pos.rstrip("%")), parse_color(color)))
        else:
            stops.append((0 if not stops else 100, parse_color(part)))
    if not stops:
        raise Refused("Give at least one colour.")
    return duration, stops


def encode_gradient(s, value):
    h = s["header"]
    duration, stops = parse_gradient(value)
    if len(stops) > 14:
        raise Refused("Up to 14 colours.")
    header = [0] * h["length"]
    header[h["repeat"]] = 1 if len(stops) == 1 else 0
    header[h["count"]] = len(stops)
    for offset in h["led_offsets"]:
        header[offset] = s["led"]
    for i, b in enumerate(le(duration, h["duration_length"])):
        header[h["duration"] + i] = b
    body = list(stops[0][1])
    last = 0
    for pos, color in stops:
        real = int(pos * 255 / 100)
        body += color + [real - last]
        last = real
    return header + body


def encode_gradient2(s, value):
    h = s["header"]
    duration, stops = parse_gradient(value)
    if len(stops) > h["max_colors"]:
        raise Refused(f"Up to {h['max_colors']} colours.")
    if duration < int(len(stops) * 33.3) or duration > 30000:
        raise Refused(f"The duration has to be from {int(len(stops) * 33.3)} to 30000 ms.")
    packet = [s["led"], 0x1D, 0x01, 0x02, 0x31, 0x51, 0xFF, 0xC8, 0x00]
    last, start = stops[0][0], stops[0][1]
    previous = list(start)
    for index, (pos, color) in enumerate(stops[1:]):
        if pos <= last:
            raise Refused("Colour positions have to go up.")
        ms = int(duration / 100 * (pos - last))
        if ms == 0:
            raise Refused("Two colours are too close together for this duration.")
        last = pos
        stage = [index, 0]
        for i, channel in enumerate(color):
            stage.append(int((channel - previous[i]) / ms * 16) & 0xFF)
            previous[i] = channel
        packet += stage + [0] + le(ms, 2)
    packet += [0] * (h["color_field_length"] - len(packet))
    split = []
    for channel in start:
        split += [(channel & 0x0F) << 4, channel >> 4]
    return packet + split + [0xFF, 0x00] + le(1500, 2) + le(650, 2) + [0, 0, 0, 0, 1, 0] + le(len(stops) - 1, 2) + le(duration, h["duration_length"])


def encode_eq(s, value):
    """A preset name, or one gain per band in dB, comma separated. Each byte is base + gain x per_db, as the device takes it."""
    text = str(value).strip().lower()
    if text in s.get("presets", {}):
        gains = s["presets"][text]
    else:
        try:
            gains = [float(g) for g in text.replace(" ", "").split(",") if g != ""]
        except ValueError:
            raise Refused(f"{value!r} isn't a preset or a list of gains. Presets: {', '.join(s.get('presets', {})) or 'none'}.") from None
    if len(gains) != s["bands"]:
        raise Refused(f"Give {s['bands']} gains, one per band, from {s['min']} to {s['max']} dB.")
    if any(not s["min"] <= g <= s["max"] for g in gains):
        raise Refused(f"Each gain goes from {s['min']} to {s['max']} dB.")
    return [int(s["base"] + g * s["per_db"]) & 0xFF for g in gains]


ENCODERS = {
    "choice": encode_choice, "number": encode_number, "number_choice": encode_number_choice, "dpi_stages": encode_dpi_stages,
    "color": encode_color, "reactive_color": encode_reactive_color, "action": encode_action, "buttons": encode_buttons,
    "gradient": encode_gradient, "gradient2": encode_gradient2, "eq": encode_eq, "per_key": encode_per_key, "zones": encode_zones,
}


def frames(setting, value=None):
    """Every report a setting sends for a value, after the report ID. Most send one; some send the same value to several
    commands (also), like one brightness per zone. Raises Refused before anything is sent."""
    kind = setting["type"]
    if kind not in ENCODERS:
        raise Refused(f"Setting this kind of value ({kind}) isn't supported yet.")
    body = ENCODERS[kind](setting, value) + list(setting.get("suffix", []))
    out = []
    for command in [setting["command"]] + list(setting.get("also", [])):
        data = list(command) + body
        if setting.get("packet_length"):
            if len(data) > setting["packet_length"]:
                raise Refused(f"That makes {len(data)} bytes, more than the {setting['packet_length']} this setting can send.")
            data += [0] * (setting["packet_length"] - len(data))
        out.append(data)
    return out


def frame(setting, value=None):
    """The bytes a setting sends for a value, after the report ID (the first report, if it sends several)."""
    return frames(setting, value)[0]


# ---- reading replies -------------------------------------------------------------------------------------------------

def decode_field(spec, reply):
    """One value from a status reply: a percentage, a yes or no, or None when its condition doesn't hold."""
    if spec["byte"] >= len(reply):
        return None
    when = spec.get("when")
    if when and reply[when["byte"]] not in when["is"]:
        return None
    raw = reply[spec["byte"]]
    if "is" in spec:
        return raw in spec["is"]
    if "not" in spec:
        return raw not in spec["not"]
    if "map" in spec:
        if raw == 0 and "zero" in spec:
            return spec["zero"]
        low, high = spec["map"]
        return max(0, min(100, round((raw - low) * 100 / (high - low))))
    return raw


def decode_battery(spec, reply):
    level = spec["level"]
    raw = reply[level["byte"]] & level.get("mask", 0xFF)
    percent = (raw + level.get("plus", 0)) * level.get("times", 1)
    charging = spec.get("charging")
    out = {"percent": max(0, min(100, percent))}
    if charging:
        out["charging"] = bool(reply[charging["byte"]] & charging.get("mask", 0xFF))
    return out


# ---- the device ------------------------------------------------------------------------------------------------------

def top_usage(descriptor):
    """(usage page, usage) of a HID report descriptor's first collection, or None."""
    page, i = None, 0
    while i < len(descriptor):
        item = descriptor[i]
        size = (0, 1, 2, 4)[item & 3]
        data = int.from_bytes(bytes(descriptor[i + 1:i + 1 + size]), "little")
        if item & 0xFC == 0x04:
            page = data
        elif item & 0xFC == 0x08 and page is not None:
            return page, data
        elif item & 0xFC == 0xA0:
            return None
        i += 1 + size
    return None


def find_node(pid, interface, usage_page=None, usage=None, hidraw=HIDRAW):
    """The hidraw node for one product: the one whose report descriptor starts with the given usage page and usage, when the
    definition names them, or else the one on the given interface."""
    for path in sorted(glob.glob(f"{hidraw}/hidraw*")):
        try:
            with open(f"{path}/device/uevent") as f:
                uevent = f.read().upper()
        except OSError:
            continue
        if f":00001038:0000{pid.upper()}" not in uevent:
            continue
        if usage_page is not None:
            try:
                with open(f"{path}/device/report_descriptor", "rb") as f:
                    top = top_usage(f.read())
            except OSError:
                continue
            if top and top[0] == usage_page and (usage is None or top[1] == usage):
                return "/dev/" + os.path.basename(path)
            continue
        intf = os.path.realpath(f"{path}/device/..")
        try:
            with open(f"{intf}/bInterfaceNumber") as f:
                number = int(f.read().strip(), 16)
        except (OSError, ValueError):
            continue
        if number == interface:
            return "/dev/" + os.path.basename(path)
    return None


def _ioc(direction, kind, number, size):
    return (direction << 30) | (size << 16) | (kind << 8) | number


class Gear:
    """One device, opened for a few commands."""

    def __init__(self, pid, families=None, fd=None):
        families = load_families() if families is None else families
        if pid not in families:
            raise GearError(f"1038:{pid} isn't covered by a definition yet.")
        self.pid = pid
        self.family, self.product = families[pid]
        self.name = self.product["name"]
        self.settings = self.family["settings"]
        self.write = os.write  # tests put a recorder here
        if fd is not None:
            self.fd = fd
            return
        node = find_node(pid, self.product["interface"], self.product.get("usage_page"), self.product.get("usage"))
        if not node:
            raise GearError(f"The {self.name} isn't plugged in, or its control interface isn't there.")
        try:
            self.fd = os.open(node, os.O_RDWR | os.O_NONBLOCK)
        except PermissionError:
            raise GearError(f"No permission for {node}. Run devices/install.sh to add the udev rule.") from None

    def close(self):
        os.close(self.fd)

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def send(self, spec, data):
        """Families marked raw list the exact bytes, report ID first, padded to their packet size. Others get report 0 added."""
        if self.family.get("raw"):
            report = bytes(list(data) + [0] * max(0, self.family.get("packet", 0) - len(data)))
        else:
            report = bytes([0] + list(data))
        while select.select([self.fd], [], [], 0)[0]:
            os.read(self.fd, 64)
        if spec.get("report", "output") == "feature" or self.family.get("feature"):
            fcntl.ioctl(self.fd, _ioc(3, ord("H"), 0x06, len(report)), report)
        else:
            self.write(self.fd, report)
        time.sleep(COMMAND_GAP)

    def reply(self, length):
        if select.select([self.fd], [], [], REPLY_TIMEOUT)[0]:
            return os.read(self.fd, max(64, length))
        raise GearError(f"The {self.name} didn't answer. If it's wireless, wake it and retry.")

    def set(self, name, value=None):
        if name not in self.settings:
            raise Refused(f"The {self.name} has no setting {name!r}. It has: {', '.join(self.settings)}.")
        spec = self.settings[name]
        reports = frames(spec, value)
        for extra in spec.get("before", []):
            self.send(spec, extra)
        for data in reports:
            self.send(spec, data)
            if spec.get("readback_length"):
                self.reply(spec["readback_length"])
        for extra in spec.get("then", []):
            self.send(spec, extra)
        remember(self.pid, name, value)
        return reports[0]

    def status(self):
        """What the device reports about itself: battery, charging, whether a wireless one is connected, ChatMix."""
        out = {}
        for read in self.family.get("status", []):
            self.send(read, read["command"])
            reply = self.reply(read["length"])
            for name, spec in read["fields"].items():
                if name == "battery" and "battery_map" in self.product:
                    spec = dict(spec, map=self.product["battery_map"])
                out[name] = decode_field(spec, reply)
        return out

    def battery(self):
        spec = self.family.get("battery")
        if not spec:
            return None
        self.send(spec, spec["command"])
        return decode_battery(spec, self.reply(spec["length"]))

    def firmware(self):
        spec = self.family.get("firmware")
        if not spec:
            return None
        self.send(spec, spec["command"])
        reply = self.reply(spec["length"])
        return ".".join(str(b) for b in reply[:spec["length"]])

    def save(self):
        spec = self.family.get("save")
        if not spec:
            raise GearError(f"The {self.name} keeps its settings without saving, or saving isn't known for it.")
        data = list(spec["command"])
        slot = spec.get("slot")
        if slot:  # save over the config that's active now, which the device reports
            self.send(slot, slot["command"])
            current = self.reply(slot["length"])[slot["byte"]]
            if not slot["first"] <= current <= slot["last"]:
                raise GearError(f"The {self.name} reported config {current}, which isn't one it has. Nothing was saved.")
            data.append(current)
        if spec.get("packet_length"):
            data += [0] * (spec["packet_length"] - len(data))
        self.send(spec, data)
        if spec.get("readback_length"):
            self.reply(spec["readback_length"])


# ---- remembered values -----------------------------------------------------------------------------------------------

def _state_path():
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return os.path.join(base, "steelmc", "devices.json")


def remembered(pid=None):
    try:
        with open(_state_path()) as f:
            data = json.load(f)
    except (OSError, ValueError):
        data = {}
    return data.get(pid, {}) if pid else data


def remember(pid, name, value):
    data = remembered()
    data.setdefault(pid, {})[name] = value
    path = _state_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=1)
    os.replace(tmp, path)
