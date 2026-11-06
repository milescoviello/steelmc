#!/bin/sh
# Put aerox and its GUI on your PATH and in the app menu, and make sure they can reach the mouse.
# Safe to run again.
set -eu

repo=$(cd "$(dirname "$(readlink -f "$0")")" && pwd)
bin="$HOME/.local/bin"
apps="$HOME/.local/share/applications"
rule=/etc/udev/rules.d/70-aerox.rules

mkdir -p "$bin" "$apps"
chmod +x "$repo/aerox.py" "$repo/gui.py"
ln -sfn "$repo/aerox.py" "$bin/aerox"
ln -sfn "$repo/gui.py" "$bin/aerox-gui"
echo "Linked $bin/aerox and $bin/aerox-gui"

sed "s|^Exec=.*|Exec=$bin/aerox-gui|" "$repo/net.local.aerox-gui.desktop" > "$apps/net.local.aerox-gui.desktop"
echo "Added \"SteelSeries settings\" to the app menu"

python3 -c 'import PySide6' 2>/dev/null || echo "The GUI needs PySide6, which isn't installed. The command line works without it."
command -v apex-analog >/dev/null || echo "The GUI's keyboard page needs apex-analog, which isn't on your PATH."

if cmp -s "$repo/udev/70-aerox.rules" "$rule"; then
    echo "The udev rule is already installed."
else
    echo "Installing a udev rule so this works without sudo (asks for your password):"
    sudo install -m 0644 "$repo/udev/70-aerox.rules" "$rule"
    sudo udevadm control --reload-rules
    sudo udevadm trigger --subsystem-match=hidraw || true
    echo "Installed. If aerox still can't open the mouse, replug the dongle."
fi
