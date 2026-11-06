# apex-analog

See how far every key is pressed on a SteelSeries Apex Pro Gen 3 on Linux, in millimetres, and wire it to anything: a game, the mouse, keystrokes, or your own program.

```
git clone https://github.com/milescoviello/apex-analog
cd apex-analog
./install.sh
apex-analog
```

Press a key slowly and watch its bar. Ctrl-C quits. There is nothing to configure and nothing else to install: it's one Python file with no dependencies.

## Use the keys as a controller

```
apex-analog gamepad
```

W is throttle, S is brake, A and D steer. They now work as a real analog controller with an Xbox 360 layout, in anything that takes one.

While it runs, those keys stop typing and everything else types as normal. Press **Pause** to get them back for a moment, like for in-game chat, and press it again to resume. Ctrl-C ends it, and nothing stays held down.

Prefer stick movement for walking around? `apex-analog map stick`.

## Map any key to anything

```
apex-analog map W=rt A=lx- D=lx+ SPACE=a
```

Each rule is `KEY=OUTPUT`.

| Output | Names |
|---|---|
| Controller sticks | `lx-` `lx+` `ly-` `ly+` `rx-` `rx+` `ry-` `ry+` |
| Controller triggers | `lt` `rt` |
| Controller buttons | `a` `b` `x` `y` `lb` `rb` `back` `start` `guide` `ls` `rs` `dpad-up` `dpad-down` `dpad-left` `dpad-right` |
| Mouse movement and scroll, faster the deeper you press | `mouse-x-` `mouse-x+` `mouse-y-` `mouse-y+` `wheel-` `wheel+` `hwheel-` `hwheel+` |
| Mouse buttons | `mouse-left` `mouse-right` `mouse-middle` |
| Keystrokes | `key:F13`, `key:LSHIFT`, `key:UP` |

Buttons and keystrokes switch on at 1 mm. Add `@mm` to choose the depth, which lets one key do two things:

```
apex-analog map W=key:W@0.5 W=key:LSHIFT@3.0     # shallow walks, deep sprints
```

More examples:

```
apex-analog map mouse                            # IJKL move the cursor, U and O scroll, H and ; click
apex-analog map drive Q=a E=b                    # a preset plus your own rules
apex-analog map drive --curve 1.5                # finer control near the top of the press
apex-analog map drive --deadzone 0.5 --full 3.0  # where the axis starts and where it reaches full
apex-analog map J=key:F13@0.5 --rapid 0.2        # re-trigger after moving 0.2 mm either way, anywhere in the travel
apex-analog map drive --no-swallow               # let the mapped keys keep typing too
```

Save a setup as a file with one rule or option per line, then use it with `apex-analog map @myprofile.txt`.

Run `apex-analog map --help` for everything.

## Wire it into your own program

**Python.** Copy `apex_analog.py` next to your script, or `pip install git+https://github.com/milescoviello/apex-analog`.

```python
import apex_analog

for keys in apex_analog.stream(keys="WASD"):
    print(keys["W"])                    # millimetres, 0.0 to 4.0
```

```python
for e in apex_analog.events():
    print(e.key, e.kind, e.velocity)    # "W down 212.4": press and release, with speed in mm/s
```

```python
from apex_analog import Keyboard, Gamepad, Mouse, VirtualKeys

with Keyboard() as kb, Gamepad() as pad:
    for keys in kb.stream(keys="WS"):
        pad.set(ly=(keys["S"] - keys["W"]) / 4)
```

`Keyboard` also has `read()`, `raw()`, `actuation()`, `actuation_icon()` and `keys`. `Mouse` and `VirtualKeys` work the same way as `Gamepad`.

**Any language, through a pipe.** One JSON object per line, holding every key currently pressed:

```
$ apex-analog stream
{"W": 2.41, "SPACE": 0.72}
$ apex-analog stream --keys WASD | jq .W
$ apex-analog stream --events
{"key": "W", "event": "down", "mm": 0.52, "velocity": 212.4, "t": 8123.4417}
```

**Over the network, on this machine:**

```
apex-analog stream --keys WASD --udp 127.0.0.1:9000     # one JSON datagram per update
apex-analog stream --keys WASD --osc 127.0.0.1:9000     # OSC, /apex/W <mm>, for music and visual tools
apex-analog stream --keys WASD --serve 8765             # WebSocket and HTTP on 127.0.0.1:8765
```

```javascript
new WebSocket("ws://127.0.0.1:8765").onmessage = e => console.log(JSON.parse(e.data).W);
```

`curl 127.0.0.1:8765` gets the latest reading. Any of these also work alongside `gamepad` and `map`, so one run can feed a game and your program together.

These tools can see everything you type, so network outputs make you choose keys with `--keys`. `--keys all` only works on this machine. `--serve` listens on 127.0.0.1 only and refuses web pages from other sites.

**Check what you've got:** `apex-analog info`, or `apex-analog info --json`, shows your access and each key's press and release points.

## Limits

- **61 keys have sensors**, the main block. ESC, the F row, nav keys and arrows don't, so they can't report depth.
- **One run at a time.** The keyboard's replies carry no ID, so two readers would mix them up. A second copy refuses to start. Give one run several outputs instead.
- **Millimetres come from the firmware's own actuation table**, not a physical measurement.
- **Network and pipe rates:** 60 updates a second by default, `--hz` to change it. Reading every key tops out near 190 a second, and reading keys from two rows near 490. Mapping defaults to 120.

## What's been tested

Apex Pro Gen 3 (`1038:1640`), firmware 4.16.8, Gentoo Linux, KDE Wayland, Python 3.14.

- The test suite (`python3 -m unittest`) needs no keyboard.
- Checked on real hardware: sensor reads, the virtual controller as seen by Linux, holding back keys, and UDP, OSC, WebSocket and HTTP clients.
- **Not yet checked:** a game running through Proton, and how fine control feels on 4 mm of travel.

## Safety

This reads the keyboard and never changes it. It can only send these five commands. Each one's firmware handler was read first and only copies memory into the reply: no flash writes, no resets, no settings changed.

| Send | Reply | |
|---|---|---|
| `D7 <bank 1-5>` | 14 raw + 14 filtered `u16` | sensors for 14 channels |
| `DA <n> [<hid> 0 0 0 0]...` | `<hid> <u16 bottom> <u16 rest>`... | calibration |
| `B1 <n> [<hid> 0 0]...` | `<hid> <press> <release>`... | actuation points, n/255 of the key's range |
| `B2 <n> [<hid> 0]...` | `<hid> <mode>`... | per-key mode byte |
| `9E` | 1 byte | OLED `ACTUATION ICON` setting |

It refuses to run on any firmware but 4.16.8, and there's no override, because on other firmware the same bytes could do something else.

**Don't probe this keyboard blindly.** Its commands include reset, reboot to bootloader, flash saves and calibration writes, and some sit one bit away from safe reads. Sending every command byte once put mine in its emergency bootloader, and it took SteelSeries GG on Windows to get it back.

Taking over the keyboard to hold back mapped keys can't lock you out: the kernel releases it the moment the program ends, however it ends.

## How depth works

Reports go to the vendor interface (usage page `0xFFC0`) as 64 bytes. Replies arrive raw on the same interface. There are 70 channels in 5 banks of 14. For each key:

```
position = (filtered - rest) / (bottom - rest)
```

Hall sensors aren't linear, so position isn't distance: half the sensor range is already 3.0 mm. `CURVE` in `apex_analog.py` holds the firmware's table for converting between them.

## Credits

Not affiliated with SteelSeries. Contains no SteelSeries firmware or code. Reverse engineered from my own keyboard and its firmware for compatibility, with AI assistance (Claude Code). MIT license.
