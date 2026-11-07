# arctis

Configure a SteelSeries Arctis 9 Wireless on Linux with no vendor software.

## Install

```
./install.sh
```

This links `arctis` into `~/.local/bin` and installs a udev rule. The rule opens only the transmitter's vendor interface. The interfaces that carry media keys stay closed. It needs only Python.

## Use

```
arctis                    # is the headset connected, its battery, ChatMix, mic and sidetone
arctis sidetone medium    # off, low, medium or high
arctis mic -6             # microphone gain, -25 to 0 dB
arctis surround on
arctis eq bass            # flat, bass, reference or smiley
arctis eq 3 2 1 0 0 0 0 1 2 3   # or ten bands, 31 Hz to 16 kHz, from -10 to 10 dB
arctis sleep 30           # turn the headset off after 30 minutes without sound, 0 for never
arctis save               # keep the settings after the transmitter loses power
arctis info               # firmware versions
arctis --json             # any of these, for scripts
```

Changes apply right away and each one is read back. They live in the transmitter's memory until it loses power. `arctis save` writes them to its flash, after asking.

## Safety

Each command it sends had its handler read in the transmitter's own firmware first. The reads only copy memory into the reply. The writes store a setting in memory and pass it on to the headset, and only `save` reaches flash. Values outside the ranges the vendor's own software uses are refused before anything is sent.

It refuses to run on any transmitter firmware but 1.1, because on other firmware the same bytes could do something else.

Never sent: the reset command, which can also wipe every setting and save that; the commands that switch the transmitter into its firmware update modes; and the ones that write device data.

## What's been tested

Arctis 9 Wireless, transmitter firmware 1.1, Gentoo Linux. The test suite (`python3 test_arctis.py`) needs no headset.

- Every read works on a real transmitter: status, versions, sidetone, mic, surround, EQ and sleep.
- Sidetone, mic gain, surround, EQ preset, EQ bands and sleep were each changed on a real transmitter, read back correctly, and put back. The headset was off at the time, so how they sound hasn't been checked yet, and nor has `save`.
