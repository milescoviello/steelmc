# steelmc-devices

Which SteelSeries devices are plugged in, and which tool here works with each.

```
steelmc-devices          # list them
steelmc-devices probe    # everything Linux knows about them
steelmc-devices --json
```

It only reads what Linux already shows in `/sys`, and sends nothing to any device, so it's safe to run on anything.

## Devices that aren't supported yet

`catalog.json` lists over 500 SteelSeries product IDs, with each one's name, what kind of device it is, and which part of it the ID belongs to: the device itself, its wireless dongle, its bootloader, and so on. Only a few are supported so far, because a tool here only sends a device commands that have been checked against that device's own firmware, and changes only turn on once someone has tried them on the real thing.

To help add one, run `steelmc-devices probe` with it plugged in and open an issue with the output. It lists the device's interfaces and HID report descriptors. That's no private information, and nothing is sent to the device to get it.
