#!/bin/sh
# Put steelmc-devices on your PATH, and let it reach the devices its definitions cover. Safe to run again.
set -eu

repo=$(cd "$(dirname "$(readlink -f "$0")")" && pwd)
bin="$HOME/.local/bin"
rule=/etc/udev/rules.d/70-steelmc-devices.rules
mkdir -p "$bin"
chmod +x "$repo/steelmc_devices.py"
ln -sfn "$repo/steelmc_devices.py" "$bin/steelmc-devices"
echo "Linked $bin/steelmc-devices"

if cmp -s "$repo/udev/70-steelmc-devices.rules" "$rule"; then
    echo "The udev rule is already installed."
else
    echo "Installing a udev rule so this works without sudo (asks for your password):"
    sudo install -m 0644 "$repo/udev/70-steelmc-devices.rules" "$rule"
    sudo udevadm control --reload-rules || true
    sudo udevadm trigger --subsystem-match=hidraw || true
    echo "Installed. If a device still can't be opened, replug it."
fi
