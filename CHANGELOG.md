# Changelog

## 1.0.0

The first release: everything here works without SteelSeries GG.

**Tried on real hardware:**
- **Apex Pro Gen 3** (`apex-analog`, `apex-oled`, the app's Keyboard page):
  - live key depth; actuation, and rapid trigger with its distance, per key;
  - rapid tap: of two keys held together, the last one pressed wins (or the one you pick), for up to 10 pairs;
  - key bindings on all three layers (normal, SteelSeries held, deeper press), the volume wheel, and which key opens the SteelSeries layer;
  - SteelSeries-layer keys that glow, in a colour you pick;
  - shortcuts, including a tap of the SteelSeries key by itself;
  - the keyboard as a game controller, with any number of axes and a SteelSeries-key toggle;
  - lighting effects and the keyboard's screen;
  - saving it all to the keyboard.
- **Aerox 9 Wireless** (`aerox`, the Mouse page): battery, DPI stages, polling, sleep, button bindings and lighting, saved to the mouse.
- **Arctis 9 Wireless** (`arctis`, the Headset page): battery, ChatMix, sidetone, mic gain, surround, sleep timer, and the equalizer as a preset or ten bands.

**Experimental, from definitions** (`steelmc-devices`, the Other devices page): settings for about 290 more SteelSeries devices:
- mice, headsets and Apex keyboards;
- QcK Prism pads, Alias mics and Arena speakers;
- the SteelSeries keyboards and lighting in MSI laptops.

None of these has been tried on a real one yet. Where GG ships the device's firmware, every command was checked against its command table, and `settings` says which firmware. Please report what works: `steelmc-devices report`, or Report how it went in the app, opens a GitHub issue with the details filled in.

**Install and update:**
- packages for Debian and Ubuntu (.deb), Fedora and openSUSE (.rpm) and Arch, attached to each release;
- `install.sh` for any distro, which gets what's missing with the distro's own packages (and PySide6 from PyPI where the distro has none);
- `steelmc-update`, which updates the way you installed, and a slim line in the app when a new release is out.

**Not yet:** the Arctis 5 and 9X, Siberia 840, Rival 150 and 160, the original Sensei, Kinzu V3, and the Stratus controllers.
