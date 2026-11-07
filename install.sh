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
has_qt() { python3 -c 'import PySide6.QtWidgets' 2>/dev/null; }
venv="$HOME/.local/share/steelmc/venv"  # PySide6 from PyPI, just for the app, where the distro doesn't package it
missing=""
command -v python3 >/dev/null 2>&1 || missing="python3"
if [ -z "$missing" ]; then
    has_qt || [ -x "$venv/bin/python" ] || missing="$missing PySide6"
    python3 -c 'import PIL' 2>/dev/null || missing="$missing Pillow"
fi
if [ -n "$missing" ]; then
    id="" id_like=""
    [ -r /etc/os-release ] && . /etc/os-release && id="${ID:-}" && id_like="${ID_LIKE:-}"
    # base: Python and Pillow (and venv, for the PyPI fallback); qt: PySide6, which not every release packages
    case " $id $id_like " in
        *" arch "*|*" manjaro "*|*" endeavouros "*) base="pacman -S --needed python python-pillow"; qt="pacman -S --needed pyside6" ;;
        *" debian "*|*" ubuntu "*) base="apt-get install -y python3 python3-pil python3-venv"; qt="apt-get install -y python3-pyside6.qtwidgets" ;;
        *" fedora "*|*" rhel "*) base="dnf install -y python3 python3-pillow"; qt="dnf install -y python3-pyside6" ;;
        *" opensuse"*|*" suse "*) base="zypper install -y python3 python3-Pillow"; qt="zypper install -y python3-pyside6" ;;
        *" gentoo "*) base="emerge --noreplace dev-python/pillow"; qt="emerge --noreplace dev-python/pyside" ;;
        *" void "*) base="xbps-install -y python3 python3-Pillow"; qt="xbps-install -y python3-pyside6" ;;
        *" alpine "*) base="apk add python3 py3-pillow"; qt="apk add py3-pyside6" ;;
        *) base=""; qt="" ;;
    esac
    echo "Missing:$missing"
    if [ -n "$base" ] && ask "Install it with your package manager (sudo $base, then PySide6)?"; then
        # shellcheck disable=SC2086
        sudo $base || echo "That didn't finish."
        # shellcheck disable=SC2086
        has_qt || sudo $qt 2>/dev/null || echo "Your distro doesn't package PySide6 for this release."
    elif [ -z "$base" ]; then
        echo "Install Python 3, PySide6 and Pillow with your package manager, then run this again."
    fi
    command -v python3 >/dev/null 2>&1 || { echo "Python 3 is needed for everything here."; exit 1; }
    if ! has_qt && [ ! -x "$venv/bin/python" ] && ask "Get PySide6 for the app from PyPI instead, into $venv?"; then
        echo "Getting PySide6 from PyPI (a few hundred MB)..."
        # PyPI's Qt brings its own libraries but not these system ones; desktops have them, minimal installs may not
        command -v apt-get >/dev/null 2>&1 && { sudo apt-get install -y libgl1 libegl1 libxkbcommon0 libfontconfig1 libdbus-1-3 >/dev/null 2>&1 || true; }
        python3 -m venv --system-site-packages "$venv" && "$venv/bin/python" -m pip install --quiet PySide6 && echo "PySide6 is ready for the app" \
            || { rm -rf "$venv"; echo "That didn't work: python3-venv may be missing. The command-line tools still work."; }
    fi
fi

# ---- the tools, their udev rules, and the app ------------------------------------------------------------------------
sh "$repo/aerox/install.sh"
sh "$repo/arctis/install.sh"
sh "$repo/devices/install.sh"
sh "$repo/apex-analog/install.sh"
bash "$repo/apex-oled/install.sh"

# apex-analog's virtual controllers need uinput; this loads it at every boot, not just now.
if [ ! -e /etc/modules-load.d/steelmc.conf ] && [ ! -e /usr/lib/modules-load.d/steelmc.conf ]; then
    { sudo mkdir -p /etc/modules-load.d && echo uinput | sudo tee /etc/modules-load.d/steelmc.conf >/dev/null && echo "uinput will load at boot"; } || true
fi

mkdir -p "$bin" "$apps"
chmod +x "$repo/gui/gui.py"
if ! has_qt && [ -x "$venv/bin/python" ]; then  # the app runs with the PySide6 from PyPI
    rm -f "$bin/steelmc"
    printf '#!/bin/sh\nexec "%s" "%s" "$@"\n' "$venv/bin/python" "$repo/gui/gui.py" > "$bin/steelmc"
    chmod +x "$bin/steelmc"
else
    ln -sfn "$repo/gui/gui.py" "$bin/steelmc"
fi
sed "s|^Exec=.*|Exec=$bin/steelmc|" "$repo/gui/net.local.steelmc.desktop" > "$apps/net.local.steelmc.desktop"
echo "Linked $bin/steelmc and added SteelMC to the app menu"
case ":$PATH:" in
    *":$bin:"*) ;;
    *) echo "Add $bin to your PATH to run these by name (most distros do it at your next login)." ;;
esac
has_qt || [ -x "$venv/bin/python" ] || echo "The app needs PySide6, which isn't installed yet. The command-line tools work without it."
echo
echo "Done. Open SteelMC from your menu, or run: steelmc"
