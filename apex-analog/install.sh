#!/bin/sh
# Put apex-analog on your PATH and make sure it can reach the keyboard. Safe to run again.
set -eu

repo=$(cd "$(dirname "$(readlink -f "$0")")" && pwd)
bin="$HOME/.local/bin"

mkdir -p "$bin"
chmod +x "$repo/apex_analog.py"
ln -sfn "$repo/apex_analog.py" "$bin/apex-analog"
echo "Linked $bin/apex-analog"
case ":$PATH:" in
    *":$bin:"*) ;;
    *) echo "Add $bin to your PATH to run apex-analog by name." ;;
esac

# Only touch the system if something is actually out of reach.
ok=1
found=0
[ -w /dev/uinput ] || ok=0
for h in /sys/class/hidraw/hidraw*; do
    if grep -qiE '00001038:0000(1640|1610|1614)' "$h/device/uevent" 2>/dev/null; then
        found=1
        { [ -r "/dev/${h##*/}" ] && [ -w "/dev/${h##*/}" ]; } || ok=0
    fi
done
for e in /sys/class/input/event*; do
    if [ "$(cat "$e/device/id/vendor" 2>/dev/null)" = 1038 ] && case "$(cat "$e/device/id/product" 2>/dev/null)" in 1640|1610|1614) true;; *) false;; esac; then
        [ -r "/dev/input/${e##*/}" ] || ok=0
    fi
done
[ "$found" = 1 ] || ok=0

if [ "$ok" = 1 ]; then
    echo "Access is already fine, no system changes needed."
else
    echo "Installing a udev rule so this works without sudo (asks for your password):"
    sudo install -m 0644 "$repo/udev/70-apex-analog.rules" /etc/udev/rules.d/70-apex-analog.rules
    sudo modprobe uinput || true
    sudo udevadm control --reload-rules
    sudo udevadm trigger --action=change --subsystem-match=hidraw --subsystem-match=input --subsystem-match=misc
    echo "Installed. If it still says no permission, replug the keyboard or log out and back in."
fi

echo
echo "Try it: apex-analog"
