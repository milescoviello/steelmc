#!/bin/sh
# Install every tool here, and the app, for you: commands in ~/.local/bin, the app in your menu. Works on any distro.
# It installs what's missing (Python, PySide6 for the app, Pillow for the keyboard's screen) with your distro's own
# packages, adds the udev rules so the devices open without sudo, and loads uinput at boot. Each step is safe to run
# again. --yes answers yes to every question.
set -eu

repo=$(cd "$(dirname "$(readlink -f "$0")")" && pwd)
bin="$HOME/.local/bin"
apps="$HOME/.local/share/applications"
yes=0
[ "${1:-}" = "--yes" ] && yes=1

ask() {  # ask "question": true for yes
    [ "$yes" = 1 ] && return 0
    printf '%s [Y/n] ' "$1"
    read -r answer </dev/tty || return 1
    case "$answer" in [nN]*) return 1 ;; *) return 0 ;; esac
}

# ---- what's missing, and the distro's way to get it ----------------------------------------------------------------
missing=""
command -v python3 >/dev/null 2>&1 || missing="python3"
if [ -z "$missing" ]; then
    python3 -c 'import PySide6.QtWidgets' 2>/dev/null || missing="$missing PySide6"
    python3 -c 'import PIL' 2>/dev/null || missing="$missing Pillow"
fi
if [ -n "$missing" ]; then
    id="" id_like=""
    [ -r /etc/os-release ] && . /etc/os-release && id="${ID:-}" && id_like="${ID_LIKE:-}"
    case " $id $id_like " in
        *" arch "*|*" manjaro "*|*" endeavouros "*) cmd="pacman -S --needed python pyside6 python-pillow" ;;
        *" debian "*|*" ubuntu "*) cmd="apt-get install -y python3 python3-pyside6.qtwidgets python3-pil" ;;
        *" fedora "*|*" rhel "*) cmd="dnf install -y python3 python3-pyside6 python3-pillow" ;;
        *" opensuse"*|*" suse "*) cmd="zypper install -y python3 python3-pyside6 python3-Pillow" ;;
        *" gentoo "*) cmd="emerge --noreplace dev-python/pyside dev-python/pillow" ;;
        *" void "*) cmd="xbps-install -y python3 python3-pyside6 python3-Pillow" ;;
        *" alpine "*) cmd="apk add python3 py3-pyside6 py3-pillow" ;;
        *) cmd="" ;;
    esac
    echo "Missing:$missing"
    if [ -n "$cmd" ] && ask "Install it with: sudo $cmd ?"; then
        # shellcheck disable=SC2086
        sudo $cmd
    elif [ -n "$cmd" ]; then
        echo "Skipped. Without PySide6 the app won't open; without Pillow the keyboard's screen won't. The rest still works."
    else
        echo "Install those with your package manager (or: python3 -m pip install --user PySide6 Pillow), then run this again."
        echo "The command-line tools only need Python."
    fi
    command -v python3 >/dev/null 2>&1 || { echo "Python 3 is needed for everything here."; exit 1; }
fi

# ---- the tools, their udev rules, and the app ------------------------------------------------------------------------
sh "$repo/aerox/install.sh"
sh "$repo/arctis/install.sh"
sh "$repo/devices/install.sh"
sh "$repo/apex-analog/install.sh"
bash "$repo/apex-oled/install.sh"

# apex-analog's virtual controllers need uinput; this loads it at every boot, not just now.
if [ ! -e /etc/modules-load.d/steelmc.conf ] && [ ! -e /usr/lib/modules-load.d/steelmc.conf ]; then
    echo uinput | sudo tee /etc/modules-load.d/steelmc.conf >/dev/null && echo "uinput will load at boot"
fi

mkdir -p "$bin" "$apps"
chmod +x "$repo/gui/gui.py"
ln -sfn "$repo/gui/gui.py" "$bin/steelmc"
sed "s|^Exec=.*|Exec=$bin/steelmc|" "$repo/gui/net.local.steelmc.desktop" > "$apps/net.local.steelmc.desktop"
echo "Linked $bin/steelmc and added SteelMC to the app menu"
case ":$PATH:" in
    *":$bin:"*) ;;
    *) echo "Add $bin to your PATH to run these by name (most distros do it at your next login)." ;;
esac
python3 -c 'import PySide6.QtWidgets' 2>/dev/null || echo "The app needs PySide6, which isn't installed yet. The command-line tools work without it."
echo
echo "Done. Open SteelMC from your menu, or run: steelmc"
