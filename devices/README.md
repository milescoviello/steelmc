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

## Settings for many more keyboards

`families/keyboards.json` covers 37 more Apex keyboards. On the Hall-effect boards (the Apex Pro Mini, Apex Pro TKL 2022 and 2024, and their wireless versions) that's the actuation point, rapid trigger and its sensitivity, and rapid tap, per key or for every key at once. Others get what their firmware takes: key colours, lighting zones, brightness, the idle dimmer and timeout, which saved config is active, which key opens the SteelSeries layer, the lock key lights, and the polling rate. The wireless boards report their battery.

```
steelmc-devices settings 1642                       # an Apex Pro TKL (2024)
steelmc-devices set 1642 actuation 1.5              # every key at 1.5 mm
steelmc-devices set 1642 actuation 1.8,w=0.5,a=0.5  # W and A at 0.5 mm, the rest at 1.8 mm
steelmc-devices set 1642 rapid_trigger on
steelmc-devices set 1612 color ff8000               # an Apex 7, every key orange
```

For 22 of these, every command was checked against the keyboard's own firmware, as SteelSeries GG ships it, before it went in: `settings` says which firmware. The rest have firmware that can't be read (or none in GG), so their commands come from GG's device files alone, and `settings` says that too. The same check marks 18 of the mice and headsets above. For a few, which USB interface they use is a guess from their siblings, so the udev rule leaves them out (a wrong guess could open the interface keystrokes travel on) and they need sudo until someone reports `steelmc-devices probe` from a real one.

## Mousepads, microphones, and a few more

`families/mousepads.json` covers the QcK Prism Cloth pads in every size and edition (the colour of each edge, and brightness; all 9 with firmware in GG checked against it) and the hard QcK Prism. `families/extras.json` covers the Alias microphone (checked against its firmware) and Alias Pro (sidetone, mute, lights, output levels, 48 V phantom power and more), the Arctis Nova Pro with its base station, the Nova 4 and 4X, and the Nova Elite and Nova Pro Omni (sidetone, microphone, noise cancelling and transparency, timers, the screen, equalizer presets and your own bands; the Omni checked against its firmware), the Siberia 150, 350, v3 Prism and [RAW] Prism, the Sims 4 headset and Dell AE2 (light colour and automatic gain), the Diablo III headset and Siberia v2 Frost Blue (light colour and mic boost), the Cyberpunk Arctis 1 Wireless, the Aerox 3 Wireless Gen 2 (polling up to 4000 Hz wireless, battery; checked against its firmware), the Sensei Wireless, Sensei [RAW] Optical, Rival 50 MSI Edition and Sims 4 mice, the Arena 7 and Arena 9 speakers (equalizer, sleep timer, and on the Arena 9 surround upmix and the screen; both checked against their firmware), and the Sims 4 Plumbob. Which USB interface these use is a guess. The speakers, microphones, pads and the Plumbob can't type, so the udev rule opens all their HID interfaces and the program finds the right one by its usage; the mice and headsets among them need sudo until someone confirms the interface with `steelmc-devices probe`.

The Apex Pro boards, the Aerox 9 Wireless and the Arctis 9 have tools of their own, which go further, and keep them.

## Devices that aren't supported yet

`catalog.json` lists over 500 SteelSeries product IDs, with each one's name, what kind of device it is, and which part of it the ID belongs to: the device itself, its wireless dongle, its bootloader, and so on. Only a few are supported so far, because a tool here only sends a device commands that have been checked against that device's own firmware, and changes only turn on once someone has tried them on the real thing.

To help add one, run `steelmc-devices probe` with it plugged in and open an issue with the output. It lists the device's interfaces and HID report descriptors. That's no private information, and nothing is sent to the device to get it.
