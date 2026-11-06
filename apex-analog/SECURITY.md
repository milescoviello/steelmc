# Security

## Reporting

Open an [issue](https://github.com/milescoviello/apex-analog/issues). If the details shouldn't be public yet, message the maintainer through your GitHub account instead. There's no private advisory process.

## What this reads and writes

- Sensor reads only touch the keyboard's vendor interface. Nothing on the keyboard changes unless you ask for it.
- `apex-analog save` is the only command that writes the keyboard's flash, and it asks first. Nothing resets or recalibrates the keyboard.
- `install.sh` installs a udev rule granting the logged-in user access (`uaccess`) to the keyboard's `hidraw` and `event` nodes and to `/dev/uinput`. Anyone logged in on this machine can then read the keyboard's sensors and create virtual devices. Run it as your own user, not root.
- Standard library only: `pip install .` pulls no third-party package, and no SteelSeries firmware or code is shipped.

## What can leak what you type

- `profile` stores per-key totals, never the order of keys, so it can't be read back as text.
- `typeprint` uses a fixed phrase, so a real password can't end up in its file.
- `macro record` stores what you press, in order, in a private file — don't type passwords during a recording.
- `stream --udp`, `--osc`, `--serve` and `overlay` can carry every key you press. `--serve` listens on 127.0.0.1 only and refuses pages from other sites. `--keys all` over the network is refused; list the keys you want.
- No audio is recorded: the `music` lighting effect listens to your output with `parec` and keeps nothing.
