#!/bin/sh
# Let the new udev rules apply to devices already plugged in, and load uinput for apex-analog's virtual controllers.
udevadm control --reload-rules 2>/dev/null || true
udevadm trigger --subsystem-match=hidraw --subsystem-match=input --subsystem-match=misc 2>/dev/null || true
modprobe uinput 2>/dev/null || true
exit 0
