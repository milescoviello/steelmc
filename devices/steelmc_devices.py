#!/usr/bin/env python3
"""steelmc-devices: which SteelSeries devices are plugged in, and which tool here works with each.

    steelmc-devices                      list them
    steelmc-devices probe                everything Linux knows about them, to paste into a report for one that isn't supported yet
    steelmc-devices report [ID]          the same, ready to send: a link to a GitHub issue with it filled in
    steelmc-devices settings ID          what can be set on a device, like steelmc-devices settings 1824
    steelmc-devices show ID              its battery and firmware where it reports them, and what was last set here
    steelmc-devices set ID SETTING VALUE change one: steelmc-devices set 1824 polling_rate 1000
    steelmc-devices save ID              keep the current settings after it loses power
    steelmc-devices ... --json

list and probe only read what Linux already shows in /sys. settings needs nothing plugged in. show, set and save talk to the device,
using only the commands its definition in families/ lists, and changing settings on these is experimental.
"""
import argparse
import glob
import json
import os
import sys
import urllib.parse

import gear

__version__ = "0.1.0"

VENDOR = "1038"
USB = "/sys/bus/usb/devices"
CATALOG = os.path.join(os.path.dirname(os.path.realpath(__file__)), "catalog.json")
# Products a dedicated tool here handles. The general definitions never take over from them.
DEDICATED = {"1640", "1610", "1614", "1858", "185a", "1874", "1876", "12c2", "12c4"}
LEVELS = {"full": "supported", "experimental": "supported, changes experimental", "read-only": "read-only for now", "part of a supported device": "part of a supported device", "paired": "the headset half of a set: its settings go through the transmitter", None: "not supported yet"}


def load_catalog(path=CATALOG):
    with open(path) as f:
        return json.load(f)["devices"]


def _read(path, binary=False):
    try:
        with open(path, "rb" if binary else "r") as f:
            data = f.read()
        return data if binary else data.strip()
    except OSError:
        return None


# HID report descriptors, decoded only as far as a report needs: which usage pages, and how big each report is.
MAIN = {0x80: "input", 0x90: "output", 0xB0: "feature"}


def describe_hid(descriptor):
    pages, reports = [], {}
    size = count = report_id = 0
    page = 0
    i = 0
    while i < len(descriptor):
        prefix = descriptor[i]
        if prefix == 0xFE:  # long item, skip it
            i += 3 + (descriptor[i + 1] if i + 1 < len(descriptor) else 0)
            continue
        length = (0, 1, 2, 4)[prefix & 3]
        value = int.from_bytes(descriptor[i + 1:i + 1 + length], "little")
        tag = prefix & 0xFC
        if tag == 0x04:
            page = value
            if page not in pages:
                pages.append(page)
        elif tag == 0x74:
            size = value
        elif tag == 0x94:
            count = value
        elif tag == 0x84:
            report_id = value
        elif tag in MAIN:
            key = (report_id, MAIN[tag])
            reports[key] = reports.get(key, 0) + size * count
        i += 1 + length
    return {
        "usage_pages": [f"{p:04x}" for p in pages],
        "vendor_defined": any(p >= 0xFF00 for p in pages),
        "reports": [{"id": rid, "kind": kind, "bytes": (bits + 7) // 8} for (rid, kind), bits in sorted(reports.items())],
    }


def interfaces(device_path):
    out = []
    for intf in sorted(glob.glob(f"{device_path}/{os.path.basename(device_path)}:*")):
        entry = {"number": _read(f"{intf}/bInterfaceNumber"), "class": _read(f"{intf}/bInterfaceClass"),
                 "subclass": _read(f"{intf}/bInterfaceSubClass"), "protocol": _read(f"{intf}/bInterfaceProtocol"), "hid": []}
        for hidraw in sorted(glob.glob(f"{intf}/*/hidraw/hidraw*")):
            descriptor = _read(f"{os.path.dirname(os.path.dirname(hidraw))}/report_descriptor", binary=True) or b""
            entry["hid"].append({"node": "/dev/" + os.path.basename(hidraw), "descriptor": descriptor.hex(),
                                 **describe_hid(descriptor)})
        out.append(entry)
    return out


def families():
    return {pid: v for pid, v in gear.load_families().items() if pid not in DEDICATED}


def scan(usb=USB, catalog=None, covered=None):
    """Every SteelSeries USB device plugged in, with what the catalog knows about it."""
    catalog = load_catalog() if catalog is None else catalog
    covered = families() if covered is None else covered
    found = []
    for path in sorted(glob.glob(f"{usb}/*")):
        if _read(f"{path}/idVendor") != VENDOR or ":" in os.path.basename(path):
            continue
        pid = (_read(f"{path}/idProduct") or "").lower()
        known = catalog.get(pid)
        support = (known or {}).get("support", {})
        if not support and pid in covered:
            support = {"level": "experimental", "tools": ["steelmc-devices"]}
        partner = (known or {}).get("paired_with")
        if not support and partner and (partner in covered or partner in DEDICATED):
            support = {"level": "paired", "tools": [f"1038:{partner}"]}
        found.append({
            "id": f"{VENDOR}:{pid}", "product": _read(f"{path}/product"), "firmware": _read(f"{path}/bcdDevice"),
            "port": os.path.basename(path), "known": known is not None,
            "name": (known or {}).get("name"), "kind": (known or {}).get("kind"), "part": (known or {}).get("part"),
            "support": support.get("level"), "tools": support.get("tools", []), "path": path,
        })
    return found


def describe(device):
    name = device["product"] or device["name"] or "Unknown SteelSeries device"
    part = device["part"] if device["part"] not in (None, "device") else ""
    level = LEVELS.get(device["support"], device["support"])
    tools = f": {', '.join(device['tools'])}" if device["tools"] else ""
    known = "" if device["known"] else ", not in the catalog"
    return f"{device['id']}  {name}{f' ({part})' if part else ''}, {device['kind'] or 'unknown kind'}, firmware {device['firmware']}{known}. {level[:1].upper()}{level[1:]}{tools}"


def probe_report(device):
    lines = [describe(device), f"  port {device['port']}"]
    if device["name"] and device["product"] and device["name"] != device["product"]:
        lines.append(f"  catalog name: {device['name']}")
    for intf in interfaces(device["path"]):
        lines.append(f"  interface {intf['number']}: class {intf['class']}, subclass {intf['subclass']}, protocol {intf['protocol']}")
        for hid in intf["hid"]:
            reports = ", ".join(f"{r['kind']} {r['bytes']} bytes" + (f" (id {r['id']})" if r["id"] else "") for r in hid["reports"])
            lines.append(f"    {hid['node']}: usage pages {' '.join(hid['usage_pages']) or 'none'}; {reports or 'no reports'}"
                         + ("; vendor-defined" if hid["vendor_defined"] else ""))
            lines.append(f"      descriptor {hid['descriptor']}")
    return "\n".join(lines)


ISSUES = "https://github.com/milescoviello/steelmc/issues/new"
URL_LIMIT = 7500  # GitHub turns away much longer links


def system_line(os_release="/etc/os-release"):
    """steelmc's version, the distro and the kernel: what a report needs to know about this computer, and nothing else."""
    pretty = ""
    for line in (_read(os_release) or "").splitlines():
        if line.startswith("PRETTY_NAME="):
            pretty = line.split("=", 1)[1].strip().strip('"')
    return f"steelmc {__version__}, {pretty or 'Linux'}, kernel {os.uname().release}"


def report(device, last=None, os_release="/etc/os-release"):
    """A device report for an issue: this computer in one line, what Linux sees of the device, and what was set here."""
    lines = [system_line(os_release), "", probe_report(device)]
    if last:
        lines += ["", "Last set here: " + ", ".join(f"{k} {v}" for k, v in last.items())]
    return "\n".join(lines)


def report_url(device, text):
    """A link that opens a new GitHub issue with the report filled in. Nothing is sent until it's submitted there."""
    name = device["product"] or device["name"] or "SteelSeries device"
    fields = {"template": "device.yml", "title": f"{name} ({device['id']})", "device": f"{name} ({device['id']})", "report": text}
    url = f"{ISSUES}?{urllib.parse.urlencode(fields)}"
    if len(url) > URL_LIMIT:  # the report descriptors are the long part
        short = [line for line in text.splitlines() if not line.lstrip().startswith("descriptor ")]
        fields["report"] = "\n".join(short) + "\n(The descriptors didn't fit in the link: steelmc-devices report prints them.)"
        url = f"{ISSUES}?{urllib.parse.urlencode(fields)}"
    return url


NO_TYPING = ("speakers", "microphone", "mousepad", "accessory", "lighting")  # kinds with no interface that can carry keystrokes


def udev_rules():
    """One rule per covered product, for the one interface its definition talks to. A product whose interface is only a
    guess gets none, since a wrong guess could open the interface its keystrokes travel on, unless it's a kind of device
    that can't type at all: then every one of its HID interfaces is opened, and the program picks the right one itself."""
    lines = ["# steelmc-devices: let the logged-in user reach the interface each device's definition uses, and only that one.",
             "# Made from families/ by: steelmc-devices udev-rules. ATTRS must match on one parent each, hence two steps."]
    for pid, (family, product) in sorted(families().items()):
        tag = f"STEELMC_{pid.upper()}"
        if product.get("interface_unconfirmed"):
            if family.get("kind") in NO_TYPING:
                lines.append(f'KERNEL=="hidraw*", SUBSYSTEM=="hidraw", ATTRS{{idVendor}}=="1038", ATTRS{{idProduct}}=="{pid}", '
                             'MODE="0660", GROUP="input", TAG+="uaccess"')
            continue
        lines.append(f'KERNEL=="hidraw*", SUBSYSTEM=="hidraw", ATTRS{{idVendor}}=="1038", ATTRS{{idProduct}}=="{pid}", ENV{{{tag}}}="1"')
        lines.append(f'ENV{{{tag}}}=="1", ATTRS{{bInterfaceNumber}}=="{product["interface"]:02x}", MODE="0660", GROUP="input", TAG+="uaccess"')
    return "\n".join(lines) + "\n"


def _gear(pid):
    pid = pid.lower().split(":")[-1]
    covered = families()
    if pid in DEDICATED:
        tool = (load_catalog().get(pid, {}).get("support", {}).get("tools") or ["its own tool"])[0]
        raise gear.GearError(f"1038:{pid} has a tool of its own here: use {tool}.")
    if pid not in covered:
        raise gear.GearError(f"1038:{pid} isn't covered yet. steelmc-devices probe shows what's needed to add it.")
    return pid, covered


def describe_setting(name, s):
    kind = s["type"]
    if kind == "choice":
        values = ", ".join(s["choices"])
    elif kind in ("number", "number_choice"):
        values = f"{s['input'][0]} to {s['input'][1]}"
    elif kind == "dpi_stages":
        values = f"up to {s['max_stages']} DPI, {s['input'][0]} to {s['input'][1]}, like 400,800,1600"
    elif kind in ("color", "color_split"):
        values = "a colour: RRGGBB or a name"
    elif kind == "reactive_color":
        values = "a colour, or off"
    elif kind == "action":
        values = "no value"
    elif kind == "buttons":
        values = "BUTTON=ACTION,..., or default. Buttons: " + ", ".join(s["buttons"])
    elif kind == "eq":
        presets = ", ".join(s.get("presets", {}))
        values = f"{s['bands']} gains from {s['min']} to {s['max']} dB, comma separated" + (f", or a preset: {presets}" if presets else "")
    elif kind in ("gradient", "gradient2"):
        values = "one colour, or [ms:]COLOR@PERCENT,..., like 2000ms:red@0,blue@50,red@100"
    elif kind == "per_key":
        if s.get("value") == "color":
            one = "a colour"
        else:
            names = list(s["values"])
            one = f"{names[0]} to {names[-1]}" if len(names) > 8 else " or ".join(names)
            one += f" {s['unit']}" if s.get("unit") else ""
        values = f"{one} for every key, then KEY=VALUE for keys that differ, like {s.get('default') or 'red'},w=..."
    elif kind == "zones":
        values = f"one colour, or {s['zones']} colours comma separated, one per zone"
    else:
        values = "not supported yet"
    return f"  {name:22} {s.get('label', '')}: {values}"


def gear_command(args):
    pid, covered = _gear(args.id)
    family, product = covered[pid]
    if args.command == "settings":
        if args.json:
            print(json.dumps({"product": product, "family": family["name"], "settings": family["settings"]}))
        else:
            print(f"{product['name']} (1038:{pid}). Changing settings is experimental: it hasn't been tried on a real one here.")
            if product.get("firmware_checked"):
                print(f"Every command here is in its firmware {product['firmware_checked']}'s command table, read from SteelSeries GG's copy.")
            else:
                print("These commands haven't been matched against its own firmware: it couldn't be read, or not every command turned up.")
            if product.get("interface_unconfirmed"):
                if family.get("kind") in NO_TYPING:
                    print("Which USB interface it uses is a guess, so it's found by its HID usage instead. steelmc-devices probe "
                          "shows the real one: please report it.")
                else:
                    print(f"Which USB interface it uses is a guess ({product['interface']}), so the udev rule leaves it out and "
                          "changing settings needs sudo for now. steelmc-devices probe shows the real one: please report it.")
            print("\n".join(describe_setting(n, s) for n, s in family["settings"].items()))
            if family.get("save"):
                print("  steelmc-devices save keeps them after it loses power.")
        return 0
    with gear.Gear(pid, covered) as g:
        if args.command == "show":
            report = {"name": g.name, "id": f"1038:{pid}", "battery": g.battery(), "firmware": g.firmware(), "status": g.status(),
                      "last_set": gear.remembered(pid)}
            if args.json:
                print(json.dumps(report))
            else:
                print(f"{g.name} (1038:{pid})")
                if report["battery"]:
                    b = report["battery"]
                    print(f"  battery   {b['percent']}%" + (", charging" if b.get("charging") else ""))
                if report["firmware"]:
                    print(f"  firmware  {report['firmware']}")
                status = report["status"]
                if status.get("connected") is False:
                    print("  headset   not connected: turn it on, or bring it closer")
                for key, label in (("battery", "battery"), ("left_battery", "left bud"), ("right_battery", "right bud")):
                    if status.get(key) is not None and status.get("connected") is not False:
                        print(f"  {label:9} {status[key]}%" + (", charging" if status.get("charging") else ""))
                if status.get("chat") is not None:
                    print(f"  ChatMix   game {status.get('game')}%, chat {status['chat']}%")
                for name, value in report["last_set"].items():
                    print(f"  {name:22} {value}   (last set here)")
            return 0
        if args.command == "set":
            sent = g.set(args.setting, args.value)
            print(f"Sent {args.setting} = {args.value if args.value is not None else '(done)'} to the {g.name}. "
                  "This is experimental: if it didn't take, please report it." + ("" if not g.family.get("save") else
                  " It lasts until the device loses power. steelmc-devices save keeps it."))
            if args.json:
                print(json.dumps({"sent": bytes(sent).hex()}))
            return 0
        if args.command == "save":
            g.save()
            print(f"Saved the {g.name}'s current settings.")
            return 0
    return 1


def main(argv=None):
    p = argparse.ArgumentParser(prog="steelmc-devices", description="Which SteelSeries devices are plugged in. Sends nothing to them.")
    p.add_argument("--version", action="version", version=__version__)
    p.add_argument("--json", action="store_true")
    sub = p.add_subparsers(dest="command")
    sub.add_parser("list", help="list them (the default)")
    pr = sub.add_parser("probe", help="everything Linux knows about them, for a report")
    pr.add_argument("id", nargs="?", help="only this one, like 1038:1652 or 1652")
    rp = sub.add_parser("report", help="a report on one, with a link that opens a GitHub issue with it filled in")
    rp.add_argument("id", nargs="?", help="like 1038:1652 or 1652; leave it out for every one plugged in")
    for name, text in (("settings", "what can be set on a device"), ("show", "battery, firmware and what was last set"),
                       ("save", "keep the current settings after it loses power")):
        sp = sub.add_parser(name, help=text)
        sp.add_argument("id", help="like 1824 or 1038:1824")
    st = sub.add_parser("set", help="change one setting")
    st.add_argument("id")
    st.add_argument("setting")
    st.add_argument("value", nargs="?")
    sub.add_parser("udev-rules", help=argparse.SUPPRESS)
    args = p.parse_args(sys.argv[1:] if argv is None else argv)
    if args.command == "udev-rules":
        sys.stdout.write(udev_rules())
        return 0
    if args.command in ("settings", "show", "set", "save"):
        try:
            return gear_command(args)
        except gear.GearError as e:
            print(f"steelmc-devices: {e}", file=sys.stderr)
            return 1
    found = scan()
    if args.command in ("probe", "report") and args.id:
        want = args.id.lower().split(":")[-1]
        found = [d for d in found if d["id"].endswith(":" + want)]
    if args.command == "report":
        if not found:
            print("No SteelSeries devices found." if not args.id else f"1038:{want} isn't plugged in.")
            return 1
        for i, d in enumerate(found):
            text = report(d, gear.remembered(d["id"].split(":")[1]))
            print(("\n" if i else "") + text + "\n\nTo send it, open this and press Submit on GitHub (nothing is sent before that):\n"
                  + report_url(d, text))
        return 0
    if args.command == "probe":
        if args.json:
            print(json.dumps([{**{k: v for k, v in d.items() if k != "path"}, "interfaces": interfaces(d["path"])} for d in found], indent=1))
        else:
            print("\n\n".join(probe_report(d) for d in found) if found else "No SteelSeries devices found.")
        return 0 if found else 1
    if args.json:
        print(json.dumps([{k: v for k, v in d.items() if k != "path"} for d in found], indent=1))
    else:
        print("\n".join(describe(d) for d in found) if found else "No SteelSeries devices found.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
