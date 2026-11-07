# steelmc-devices

Which SteelSeries devices are plugged in, and which tool here works with each.

```
steelmc-devices          # list them
steelmc-devices probe    # everything Linux knows about them
steelmc-devices --json
```

It only reads what Linux already shows in `/sys`, and sends nothing to any device, so it's safe to run on anything.

## Settings for many more mice

`families/` holds definitions for over 70 more SteelSeries mice: Rival, Sensei, Prime, Kana and Kinzu, and the Aerox 3 and Aerox 5, wired and wireless. They say which commands set each thing and how its value is sent. Depending on the mouse that's DPI stages, polling rate, colours and lighting effects, button mappings, brightness, sleep and dim timers, battery and saving.

```
steelmc-devices settings 1824            # what a Rival 3 can be set to; nothing needs to be plugged in
steelmc-devices set 1824 sensitivity 400,800,1600
steelmc-devices set 1824 logo_color ff8000
steelmc-devices set 1824 buttons_mapping button4=f13,button5=disable
steelmc-devices show 1824                # battery and firmware where it reports them, and what was last set here
steelmc-devices save 1824                # keep it after the mouse loses power, on mice that can
```

Only the commands a definition lists are ever sent, and every value is checked first. Most of these mice can't report their settings back, so the last values set here are kept in `~/.config/steelmc/devices.json`. **Changing settings on these is experimental**: the definitions come from how these mice are known to work, but nobody has tried them on a real one here. Please report what worked.

## Settings for many more headsets

`families/headsets.json` covers 36 more Arctis models: the Arctis 1 and 7 Wireless lines, the Arctis 7+, the Arctis Pro Wireless and GameDAC, and the Nova 3, 3P, 5, 7, 7P and Pro Wireless, with their special editions. Depending on the model that's sidetone, mic volume and mute light, the equalizer (presets or your own bands), volume limiter, lights, the sleep timer and Bluetooth options. `show` reports what the headset says about itself: whether it's connected, its battery and charging, and ChatMix.

```
steelmc-devices settings 2202                 # an Arctis Nova 7
steelmc-devices set 2202 sidetone medium
steelmc-devices set 2202 equalizer bass
steelmc-devices set 2202 equalizer 2,1,0,0,0,0,0,1,2,3
steelmc-devices show 2202
```

The Apex Pro boards, the Aerox 9 Wireless and the Arctis 9 have tools of their own, which go further, and keep them.

## Devices that aren't supported yet

`catalog.json` lists over 500 SteelSeries product IDs, with each one's name, what kind of device it is, and which part of it the ID belongs to: the device itself, its wireless dongle, its bootloader, and so on. Only a few are supported so far, because a tool here only sends a device commands that have been checked against that device's own firmware, and changes only turn on once someone has tried them on the real thing.

To help add one, run `steelmc-devices probe` with it plugged in and open an issue with the output. It lists the device's interfaces and HID report descriptors. That's no private information, and nothing is sent to the device to get it.
