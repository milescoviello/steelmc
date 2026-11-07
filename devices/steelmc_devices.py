#!/usr/bin/env python3
"""steelmc-devices: which SteelSeries devices are plugged in, and which tool here works with each.

    steelmc-devices             list them
    steelmc-devices probe       everything Linux knows about them, to paste into a report for a device that isn't supported yet
    steelmc-devices ... --json

This only reads what Linux already shows in /sys. It sends nothing to any device, so it's safe on anything.
"""
import argparse
import glob
import json
import os
import sys

__version__ = "0.1.0"

VENDOR = "1038"
USB = "/sys/bus/usb/devices"
CATALOG = os.path.join(os.path.dirname(os.path.realpath(__file__)), "catalog.json")
LEVELS = {"full": "supported", "read-only": "read-only for now", "part of a supported device": "part of a supported device", None: "not supported yet"}


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


def scan(usb=USB, catalog=None):
    """Every SteelSeries USB device plugged in, with what the catalog knows about it."""
    catalog = load_catalog() if catalog is None else catalog
    found = []
    for path in sorted(glob.glob(f"{usb}/*")):
        if _read(f"{path}/idVendor") != VENDOR or ":" in os.path.basename(path):
            continue
        pid = (_read(f"{path}/idProduct") or "").lower()
        known = catalog.get(pid)
        support = (known or {}).get("support", {})
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


def main(argv=None):
    p = argparse.ArgumentParser(prog="steelmc-devices", description="Which SteelSeries devices are plugged in. Sends nothing to them.")
    p.add_argument("--version", action="version", version=__version__)
    p.add_argument("--json", action="store_true")
    sub = p.add_subparsers(dest="command")
    sub.add_parser("list", help="list them (the default)")
    pr = sub.add_parser("probe", help="everything Linux knows about them, for a report")
    pr.add_argument("id", nargs="?", help="only this one, like 1038:1652 or 1652")
    args = p.parse_args(sys.argv[1:] if argv is None else argv)
    found = scan()
    if args.command == "probe":
        if args.id:
            want = args.id.lower().split(":")[-1]
            found = [d for d in found if d["id"].endswith(":" + want)]
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
