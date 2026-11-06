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
| A second controller | put `p2:` in front: `I=p2:rt`, `J=p2:lx-`, `K=p2:a` |
| Pedals on their own device, with no default bindings | `clutch` `handbrake` `pedal3` `pedal4` `pedal5` `pedal6` |
| A keystroke that repeats faster the deeper you press | `tap:RIGHT` |
| Desktop controls, faster the deeper you press | `volume+` `volume-` `brightness+` `brightness-` |
| Any command, repeating the same way | `run:your-command` |

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
apex-analog map drive2                           # two players on one keyboard: WASD and IJKL. stick2 is the walking version
apex-analog map manual                           # drive, plus LSHIFT as an analog clutch and SPACE as an analog handbrake
apex-analog map "]=volume+" "[=volume-"          # hold harder to change volume faster
apex-analog map "X=tap:LEFT" "C=tap:RIGHT"       # scrub through a video, faster the deeper you press
```

`manual` puts the clutch and handbrake on a separate pedals device, so they can't collide with any controller bindings. Bind those two axes once in your game's controls menu.

Save a setup as a file with one rule or option per line, then use it with `apex-analog map @myprofile.txt`.

Run `apex-analog map --help` for everything.

## Play it as an instrument

```
apex-analog midi
```

This opens a MIDI port called `Apex Analog`. Connect it from your DAW or synth, or with `aconnect`. No sound comes out until something is listening.

The Z row and Q row are laid out like piano keys, with the black keys on the row above each. How fast you press sets how loud the note starts, and how deep you hold it sends pressure for that one note. SPACE sustains, LSHIFT is the mod wheel, and TAB and CAPS bend pitch up and down.

```
apex-analog midi --layout grid       # every key a note, rows a fourth apart, like a guitar: about 4 octaves
apex-analog midi --octave 1          # everything up an octave
apex-analog midi --cc F=74           # F's depth also sends controller 74
apex-analog midi --aftertouch channel  # for synths that don't take per-note pressure
apex-analog midi --soft 5 --hard 200 # tune how press speed maps to loudness
```

## Press deep for capitals

```
apex-analog type
```

Type lightly for lowercase, and press past 3.2 mm for a capital or the other symbol on that key, with no Shift. A key's keystroke goes out once its depth is settled, and keys always go out in the order you pressed them. Holding Ctrl, Alt or Win keeps shortcuts instant.

Choose something else for a deep press, per key, with `--combo`:

```
apex-analog type --combo J=LCTRL+J --combo K=F13
```

## Make keys glow

```
apex-analog lights
```

The whole keyboard goes dark, then each key glows brighter the deeper you press it and fades when you let go. Ctrl-C puts your normal lighting back. Add `--lights` to `gamepad`, `map` or `midi` to make their keys glow while you use them.

```
apex-analog lights --color 00C8FF --fade 0.5
apex-analog lights --base 0A0A14     # keep the rest of the board dimly lit instead of dark
apex-analog lights --keys WASD
apex-analog lights --off             # normal lighting again, if something quit without restoring it
```

## Change where keys actuate

No GG needed.

```
apex-analog actuation                # see what every key is set to
apex-analog actuation W 1.0          # W fires at 1.0 mm
apex-analog actuation WASD 1.2
apex-analog actuation all 1.8        # the level every key on this board was on before any changes
```

It uses the nearest of the firmware's 20 levels and reads the result back to check it. LGUI can't be changed this way.

The change lives in the keyboard's memory. What happens to it after a replug hasn't been tested yet.

## Put it on your stream

```
apex-analog overlay
```

Add `http://127.0.0.1:8765/overlay` as an OBS browser source. It shows W A S D, SPACE, LSHIFT and LCTRL filling up as you press them, on a transparent background. Choose keys with `--keys`. Viewers can read anything you show, so be careful with `--keys all`.

## Learn how you type

```
apex-analog profile
```

Type normally for a while, then press Ctrl-C. It tells you how deep and fast you press, how often you bottom out, how many presses stopped short of your actuation point, and what that means for your settings. Runs add together. `--report` shows the totals and `--reset` clears them. It stores per-key totals only, never the order of keys, so it can't be read back as text.

## Check your switches

```
apex-analog health               # rest drift, noise and range on every switch, keep your hands off for 2 seconds
apex-analog health --press 60    # also press every key once to find any that don't reach the bottom
```

## Recognise you by how you press

An experiment, not security.

```
apex-analog typeprint enroll     # type a set phrase 8 times
apex-analog typeprint check      # try to pass, then see whether anyone else can
apex-analog typeprint forget     # delete it
```

It compares how deep, how fast and how long you press each key, and the gaps between keys. `check` exits 0 on a match and 1 otherwise. The phrase is fixed on purpose, so a real password can't end up in its file.

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
- **Online games:** virtual controllers and rapid-trigger keys can break the rules of games with anti-cheat. Check before using them there.- **Network and pipe rates:** 60 updates a second by default, `--hz` to change it. Reading every key tops out near 190 a second, and reading keys from two rows near 490. Mapping defaults to 120.

## What's been tested

Apex Pro Gen 3 (`1038:1640`), firmware 4.16.8, Gentoo Linux, KDE Wayland, Python 3.14.

- The test suite (`python3 -m unittest`) needs no keyboard.
- Checked on real hardware: sensor reads, the virtual controller as seen by Linux, holding back keys, and UDP, OSC, WebSocket and HTTP clients.
- A real drive: BeamNG.drive 0.40.4 through Proton saw it as `XBox Controller 1` with no setup, and it drove well on W, S, A and D. That's one person and one short session.
- The typing print passed on real key presses, `health` ran on the real keyboard, and an actuation change on W was read back and felt on a real press.
- **Tested only with simulated key depths so far:** MIDI playing feel, pressure typing, the typing profile's numbers, and two players or pedals inside a game. MIDI did reach a real ALSA client, the overlay did run in a real browser, and the extra controllers and pedals did appear to Linux as joysticks.
- **Not yet run on the real keyboard:** `lights`, and `actuation` as a command. Both send bytes checked by tests against the firmware's format.

## Safety

Unless you run `lights` or set `actuation`, this only reads the keyboard. Every command it can send is listed here, and each one's firmware handler was read first. None of them reach a flash save or a reset.

These five only copy memory into the reply:

| Send | Reply | |
|---|---|---|
| `D7 <bank 1-5>` | 14 raw + 14 filtered `u16` | sensors for 14 channels |
| `DA <n> [<hid> 0 0 0 0]...` | `<hid> <u16 bottom> <u16 rest>`... | calibration |
| `B1 <n> [<hid> 0 0]...` | `<hid> <press> <release>`... | actuation points, n/255 of the key's range |
| `B2 <n> [<hid> 0]...` | `<hid> <mode>`... | per-key mode byte |
| `9E` | 1 byte | OLED `ACTUATION ICON` setting |

These three change something in the keyboard's memory, and only when you ask:

| Send | | Used by |
|---|---|---|
| `3A <n> [<hid> <r> <g> <b>]...` | colours for up to 15 keys | `lights` |
| `3B` | back to normal lighting | `lights` |
| `31 <n> [<hid> <press> <release>]...` | actuation for up to 20 keys, as n/255 of the key's range | `actuation` |

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
