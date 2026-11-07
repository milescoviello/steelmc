#!/bin/sh
# Put arctis on your PATH and make sure it can reach the transmitter. Safe to run again.
set -eu

repo=$(cd "$(dirname "$(readlink -f "$0")")" && pwd)
bin="$HOME/.local/bin"
rule=/etc/udev/rules.d/70-arctis.rules

mkdir -p "$bin"
chmod +x "$repo/arctis.py"
ln -sfn "$repo/arctis.py" "$bin/arctis"
echo "Linked $bin/arctis"

if cmp -s "$repo/udev/70-arctis.rules" "$rule"; then
    echo "The udev rule is already installed."
else
    echo "Installing a udev rule so this works without sudo (asks for your password):"
    sudo install -D -m 0644 "$repo/udev/70-arctis.rules" "$rule"
    sudo udevadm control --reload-rules || true
    sudo udevadm trigger --subsystem-match=hidraw || true
    echo "Installed. If arctis still can't open the transmitter, replug it."
fi
