# Change log

## Unreleased

- `lights --effect heatmap` now updates as you type, instead of showing the totals from when it started

## 0.1.0 — 2026-11-05

First release. Reads how far each key is pressed on a SteelSeries Apex Pro Gen 3 (firmware 4.16.8) and turns that into anything:

- `apex-analog` live bars; `stream` as JSON on a pipe, over UDP, over OSC, and as WebSocket plus HTTP on 127.0.0.1
- `gamepad`, `map`, and the presets `drive`, `drive2`, `manual`, `stick`, `mouse`: analog controller axes, triggers and buttons, pedals on their own device, a second controller, mouse movement and buttons, keystrokes, macros, and your own commands
- `midi` with velocity from press speed, per-note pressure, pitch bend, mod wheel, piano or `--layout grid`
- `type` for capitals on a deep press, and `--combo` to choose something else per key
- `lights`, `paint` from any program, and the effects `ripple`, `music`, `stats`, `rev`, `heatmap`
- `actuation`, `rapid`, `preset`, `save` to the keyboard's flash, `auto` by focused window, `macro`, `oled`, `magnet`
- `profile`, `health`, `typeprint`, `overlay`, `info`, `status`, `off`, `stop`, `autostart`
- a background daemon, so long-running commands become jobs, several run at once, and a replug reconnects them

`python3 -m unittest` runs 132 tests with no keyboard attached. The README's "What's been tested" names what was checked on real hardware and what is still only simulated.
