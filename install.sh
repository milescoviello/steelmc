#!/bin/sh
# Install every tool here, and the GUI. Each step is safe to run again, and may ask for your password
# to add a udev rule.
set -eu

repo=$(cd "$(dirname "$(readlink -f "$0")")" && pwd)
bin="$HOME/.local/bin"
apps="$HOME/.local/share/applications"

sh "$repo/aerox/install.sh"
sh "$repo/arctis/install.sh"
sh "$repo/apex-analog/install.sh"
bash "$repo/apex-oled/install.sh"

mkdir -p "$bin" "$apps"
chmod +x "$repo/gui/gui.py"
ln -sfn "$repo/gui/gui.py" "$bin/steelmc"
sed "s|^Exec=.*|Exec=$bin/steelmc|" "$repo/gui/net.local.steelmc.desktop" > "$apps/net.local.steelmc.desktop"
echo "Linked $bin/steelmc and added SteelMC to the app menu"
python3 -c 'import PySide6' 2>/dev/null || echo "The GUI needs PySide6, which isn't installed. The command-line tools work without it."
