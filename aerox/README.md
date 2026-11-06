# aerox

Read a SteelSeries Aerox 9 Wireless from Linux, with no vendor software: battery, DPI stages, polling rate, sleep time and button bindings.

It's one Python file with no dependencies, and it's **read-only** for now.

```
$ aerox
Aerox 9 Wireless, through the dongle (/dev/hidraw8)
  dongle    1.2.4
  mouse     1.2.4
  battery   30% (level 7 of 21), not charging
  polling   1000 Hz
  dpi       stage 1 of 5: [0x04] 0x09 0x0d 0x1b 0x26
  sleep     5 min
```

## Install

```
./install.sh
```

This links `aerox` into `~/.local/bin` and adds a udev rule. The rule opens only the mouse's vendor interface (interface 3). The interfaces carrying clicks and keystrokes stay closed.

## Use

| Command | Shows |
|---|---|
| `aerox` | Status |
| `aerox battery` | Battery |
| `aerox dpi` | DPI stages, current one in brackets |
| `aerox polling` | Polling rate |
| `aerox sleep` | Sleep time |
| `aerox buttons` | What each button is bound to, and which differ from the default |
| `aerox info` | Device node, firmware versions, git hashes |

Add `--json` to any of them for scripts.

## What the values mean

- **DPI** is shown raw. The value goes straight to a sensor register and the firmware has no conversion table, so the tool doesn't guess.
- **Battery** is a level from 1 to 21. The percentage assumes 5% steps.
- **Button names** come from the firmware's defaults: `side N` is the button that types key N out of the box. `record 3` and `record 4` are physical buttons not identified yet.

## Safety

The protocol comes from reading the firmware's command handlers. Commands look harmless by their shape but aren't: some with the "read" bit set restore defaults and save to flash. So:

- It can only send the exact bytes and arguments in `ALLOWED`. Anything else is refused before reaching the device.
- It stops on any firmware other than 1.2.4, on both the dongle and the mouse.
- It sends one command at a time and waits for the reply, since the dongle drops anything sent while a forwarded command is pending.

## Tested

On real hardware through the dongle: every command above. Not tested yet: the mouse plugged in by cable, or the mouse asleep or off.

```
python3 test_aerox.py
```

The tests need no hardware.
