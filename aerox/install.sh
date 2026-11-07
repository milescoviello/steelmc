#!/bin/sh
# Put aerox on your PATH and make sure it can reach the mouse. Safe to run again.
set -eu

repo=$(cd "$(dirname "$(readlink -f "$0")")" && pwd)
bin="$HOME/.local/bin"
rule=/etc/udev/rules.d/70-aerox.rules

mkdir -p "$bin"
chmod +x "$repo/aerox.py"
ln -sfn "$repo/aerox.py" "$bin/aerox"
echo "Linked $bin/aerox"

if cmp -s "$repo/udev/70-aerox.rules" "$rule"; then
    echo "The udev rule is already installed."
else
    echo "Installing a udev rule so this works without sudo (asks for your password):"
    sudo install -m 0644 "$repo/udev/70-aerox.rules" "$rule"
    sudo udevadm control --reload-rules || true
    sudo udevadm trigger --subsystem-match=hidraw || true
    echo "Installed. If aerox still can't open the mouse, replug the dongle."
fi
