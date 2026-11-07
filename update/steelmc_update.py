#!/usr/bin/env python3
"""steelmc-update: update steelmc the way it was installed.

From a git clone it pulls and runs install.sh again. From a package (.deb, .rpm or Arch), it fetches the newest GitHub
release's package for this system, checks it against the release's SHA256SUMS, and installs it with the package manager.
From `make install` it fetches the release's source and runs make install again. It only ever talks to GitHub.
"""
import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

REPO = "milescoviello/steelmc"
API = f"https://api.github.com/repos/{REPO}/releases/latest"
HERE = os.path.dirname(os.path.realpath(__file__))
ROOT = os.path.dirname(HERE)
TIMEOUT = 15


class UpdateError(Exception):
    pass


def installed_version(root=ROOT):
    """The suite's version: the one steelmc-devices reports."""
    path = os.path.join(root, "devices", "steelmc_devices.py")
    try:
        with open(path) as f:
            match = re.search(r'^__version__ = "([^"]+)"', f.read(), re.M)
    except OSError:
        match = None
    return match.group(1) if match else "0"


def parse_version(text):
    """'v1.2.3' or '1.2.3' -> (1, 2, 3). Anything after the numbers is ignored."""
    match = re.match(r"v?(\d+(?:\.\d+)*)", str(text).strip())
    return tuple(int(p) for p in match.group(1).split(".")) if match else (0,)


def newer(latest, current):
    return parse_version(latest) > parse_version(current)


def _get(url, accept="application/vnd.github+json"):
    request = urllib.request.Request(url, headers={"Accept": accept, "User-Agent": "steelmc-update"})
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            return response.read()
    except urllib.error.HTTPError as e:
        if e.code == 404 and url == API:
            raise UpdateError("No release has been published yet.") from None
        raise UpdateError(f"GitHub said {e.code} for {url}") from None
    except OSError as e:
        raise UpdateError(f"Couldn't reach GitHub: {e}") from None


def latest_release(fetch=_get):
    """{'version': '1.2.3', 'notes': '...', 'assets': {name: url}, 'url': page} for the newest release."""
    data = json.loads(fetch(API))
    return {"version": data["tag_name"].lstrip("v"), "notes": data.get("body") or "", "url": data.get("html_url", ""),
            "tarball": data.get("tarball_url", ""), "assets": {a["name"]: a["browser_download_url"] for a in data.get("assets", [])}}


def install_kind(root=ROOT, run=subprocess.run):
    """How this copy got here: 'git', 'deb', 'rpm', 'arch', 'aur-git' (steelmc-git, which its AUR helper updates),
    'make' (make install, so root is PREFIX/lib/steelmc) or 'copy' (none of those, such as an unpacked zip)."""
    if os.path.isdir(os.path.join(root, ".git")):
        return "git"
    probe = os.path.join(root, "devices", "steelmc_devices.py")
    for kind, argv in (("deb", ["dpkg", "-S", probe]), ("rpm", ["rpm", "-qf", probe]), ("arch", ["pacman", "-Qo", probe])):
        if shutil.which(argv[0]):
            result = run(argv, capture_output=True, text=True)
            if result.returncode == 0 and "not owned" not in result.stdout + result.stderr:
                owner = re.search(r"is owned by (\S+)", result.stdout)
                if kind == "arch" and owner and owner.group(1).endswith("-git"):
                    return "aur-git"
                return kind
    if os.path.basename(root) == "steelmc" and os.path.basename(os.path.dirname(root)) == "lib":
        return "make"
    return "copy"


ASSET_PATTERNS = {"deb": r"\.deb$", "rpm": r"\.rpm$", "arch": r"\.pkg\.tar\.zst$"}


def pick_asset(kind, assets):
    pattern = ASSET_PATTERNS[kind]
    names = [n for n in assets if re.search(pattern, n)]
    if not names:
        raise UpdateError(f"The newest release has no {kind} package. Get it from https://github.com/{REPO}/releases")
    return names[0]


def checksums(text):
    """SHA256SUMS lines -> {name: hash}."""
    out = {}
    for line in text.splitlines():
        parts = line.split()
        if len(parts) == 2:
            out[parts[1].lstrip("*")] = parts[0].lower()
    return out


def download(name, assets, folder, fetch=_get):
    """Fetch one asset and check it against the release's SHA256SUMS. Returns its path."""
    if "SHA256SUMS" not in assets:
        raise UpdateError("The release has no SHA256SUMS, so the download can't be checked. Nothing was installed.")
    sums = checksums(fetch(assets["SHA256SUMS"], "application/octet-stream").decode())
    data = fetch(assets[name], "application/octet-stream")
    if hashlib.sha256(data).hexdigest() != sums.get(name):
        raise UpdateError(f"{name} doesn't match its checksum. Nothing was installed.")
    path = os.path.join(folder, name)
    with open(path, "wb") as f:
        f.write(data)
    return path


def elevate(argv, graphical=False):
    """Run as root: sudo in a terminal, pkexec from the app."""
    if os.geteuid() == 0:
        return argv
    if graphical and shutil.which("pkexec"):
        return ["pkexec", *argv]
    return ["sudo", *argv]


def install_package(kind, path, graphical=False):
    if kind == "deb":
        argv = ["apt-get", "install", "-y", path] if shutil.which("apt-get") else ["dpkg", "-i", path]
    elif kind == "rpm":
        argv = (["dnf", "install", "-y", path] if shutil.which("dnf") else
                ["zypper", "--non-interactive", "install", "--allow-unsigned-rpm", path] if shutil.which("zypper") else
                ["rpm", "-U", path])
    else:
        argv = ["pacman", "-U", "--noconfirm", path]
    return elevate(argv, graphical)


def update(check_only=False, yes=False, graphical=False, root=ROOT, fetch=_get, run=subprocess.run, say=print):
    """Returns 0 when up to date or updated, 10 when --check finds a newer release, 1 on failure."""
    kind = install_kind(root, run)
    if kind == "git":
        if check_only:
            run(["git", "-C", root, "fetch", "--quiet"])
            behind = run(["git", "-C", root, "rev-list", "--count", "HEAD..@{u}"], capture_output=True, text=True).stdout.strip()
            if behind and behind != "0":
                say(f"{behind} new commits are waiting. steelmc-update gets them.")
                return 10
            say("Up to date.")
            return 0
        if run(["git", "-C", root, "pull", "--ff-only"]).returncode != 0:
            raise UpdateError("git pull didn't work. Is something changed in the clone? git status shows what.")
        run([os.path.join(root, "install.sh")] + (["--yes"] if yes else []))
        say("Updated. Restart the app, and run apex-analog stop so its daemon starts again on the new code.")
        return 0
    if kind == "aur-git":
        raise UpdateError("This is steelmc-git from the AUR: update it with your AUR helper (yay -S steelmc-git, say).")
    if kind == "copy" and not check_only:
        raise UpdateError(f"This copy didn't come from git, a package or make install, so it can't update itself. "
                          f"Get the new one from https://github.com/{REPO}/releases, or clone it with git.")
    current = installed_version(root)
    release = latest_release(fetch)
    if not newer(release["version"], current):
        say(f"Up to date ({current}).")
        return 0
    say(f"steelmc {release['version']} is out (you have {current}). {release['url']}")
    if check_only:
        return 10
    if not yes and sys.stdin.isatty():
        if input("Update now? [Y/n] ").strip().lower().startswith("n"):
            return 0
    with tempfile.TemporaryDirectory() as folder:
        if kind == "make":
            tarball = os.path.join(folder, "steelmc.tar.gz")
            with open(tarball, "wb") as f:
                f.write(fetch(release["tarball"], "application/octet-stream"))
            run(["tar", "-xzf", tarball, "-C", folder], check=True)
            source = next(os.path.join(folder, d) for d in os.listdir(folder) if os.path.isdir(os.path.join(folder, d)))
            prefix = os.path.dirname(os.path.dirname(root))  # PREFIX/lib/steelmc -> PREFIX
            argv = elevate(["make", "-C", source, f"PREFIX={prefix}", "install"], graphical)
        else:
            path = download(pick_asset(kind, release["assets"]), release["assets"], folder, fetch)
            argv = install_package(kind, path, graphical)
        if run(argv).returncode != 0:
            raise UpdateError("The package manager didn't finish. Nothing else was changed.")
    say(f"Updated to {release['version']}. Restart the app, and run apex-analog stop so its daemon starts again on the new code.")
    return 0


# ---- the app's quiet daily check ---------------------------------------------------------------------------------------

def _state_path():
    base = os.environ.get("XDG_STATE_HOME") or os.path.expanduser("~/.local/state")
    return os.path.join(base, "steelmc", "update.json")


def load_state():
    try:
        with open(_state_path()) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_state(state):
    path = _state_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path + ".tmp", "w") as f:
        json.dump(state, f)
    os.replace(path + ".tmp", path)


def check_for_app(now=None, fetch=_get, root=ROOT, run=subprocess.run):
    """The version to offer in the app's banner, or None: no newer release, already dismissed, checked in the last day,
    turned off with STEELMC_NO_UPDATE_CHECK, or offline. Never raises."""
    if os.environ.get("STEELMC_NO_UPDATE_CHECK"):
        return None
    now = time.time() if now is None else now
    state = load_state()
    try:
        if "checked" not in state or now - state["checked"] >= 86400:
            if install_kind(root, run) in ("git", "aur-git", "copy"):
                return None  # these update another way (git, the AUR helper, by hand): the Update button can't
            state["latest"] = latest_release(fetch)["version"]
            state["checked"] = now
            save_state(state)
    except (UpdateError, ValueError, KeyError, OSError):
        return None
    latest = state.get("latest")
    if latest and newer(latest, installed_version(root)) and state.get("dismissed") != latest:
        return latest
    return None


def dismiss(version):
    state = load_state()
    state["dismissed"] = version
    save_state(state)


def main(argv=None):
    p = argparse.ArgumentParser(prog="steelmc-update", description="Update steelmc the way it was installed.")
    p.add_argument("--check", action="store_true", help="only say whether there's an update (exit 10 if there is)")
    p.add_argument("--yes", action="store_true", help="don't ask")
    p.add_argument("--graphical", action="store_true", help=argparse.SUPPRESS)  # the app: ask for the password with pkexec
    args = p.parse_args(argv)
    try:
        return update(args.check, args.yes, args.graphical)
    except UpdateError as e:
        print(f"steelmc-update: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
