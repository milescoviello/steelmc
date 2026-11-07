#!/bin/sh
# Put steelmc-devices on your PATH. It only reads /sys, so it needs no udev rule. Safe to run again.
set -eu

repo=$(cd "$(dirname "$(readlink -f "$0")")" && pwd)
bin="$HOME/.local/bin"
mkdir -p "$bin"
chmod +x "$repo/steelmc_devices.py"
ln -sfn "$repo/steelmc_devices.py" "$bin/steelmc-devices"
echo "Linked $bin/steelmc-devices"
