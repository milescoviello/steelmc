# Contributing

## Tests

```
python3 -m unittest
```

132 tests, and no keyboard needed: the hardware-only groups skip when `/dev/uinput` or the ALSA sequencer isn't reachable. Python 3.8 or newer. CI runs the suite on 3.8, 3.11 and 3.14, and parses the file against the 3.8 grammar so it stays compatible.

## On real hardware

```
./install.sh          # symlink on PATH, plus a udev rule if access is missing
apex-analog info      # the keyboard, your access, and every key's settings
apex-analog health    # rest drift, noise and range on every switch; keep your hands off for 2 seconds
```

The tool refuses to run on firmware other than 4.16.8, with no override, because on other firmware the same bytes could mean something else.

## Don't send commands the tool doesn't already send

The keyboard's command set includes reset, reboot to bootloader, flash saves and calibration writes, and some commands sit one bit away from safe reads. Sending every command byte once put one keyboard into its emergency bootloader, and it took SteelSeries GG on Windows to get it back. If you add a command, read its firmware handler first and add it to the Safety table in the README.

## House rules

- `apex_analog.py` stays one file, standard library only.
- README claims stay honest: say what was tested on hardware and what is only simulated.
- Commit messages say what changed in behaviour, not what was refactored.
