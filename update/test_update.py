#!/usr/bin/env python3
"""Tests for steelmc-update. Nothing here touches the network or installs anything."""
import hashlib
import json
import os
import subprocess
import tempfile
import unittest
from unittest import mock

import steelmc_update as up


def release(version="9.9.9", assets=None):
    return json.dumps({"tag_name": f"v{version}", "body": "notes", "html_url": "https://example/release",
                       "tarball_url": "https://example/tarball",
                       "assets": [{"name": n, "browser_download_url": f"https://example/{n}"} for n in (assets or [])]}).encode()


class Versions(unittest.TestCase):
    def test_compare(self):
        self.assertTrue(up.newer("1.10.0", "1.9.9"))
        self.assertTrue(up.newer("v2.0", "1.99"))
        self.assertFalse(up.newer("1.0.0", "1.0.0"))
        self.assertFalse(up.newer("1.0.0", "1.0.1"))
        self.assertEqual(up.parse_version("1.2.3-rc1"), (1, 2, 3))

    def test_installed_version_is_steelmc_devices(self):
        here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        self.assertRegex(up.installed_version(here), r"^\d+\.\d+\.\d+$")


class Packages(unittest.TestCase):
    def test_picks_the_package_for_this_system(self):
        assets = {"steelmc_1.0.0_all.deb": "u1", "steelmc-1.0.0-1.noarch.rpm": "u2", "steelmc-1.0.0-1-any.pkg.tar.zst": "u3", "SHA256SUMS": "u4"}
        self.assertEqual(up.pick_asset("deb", assets), "steelmc_1.0.0_all.deb")
        self.assertEqual(up.pick_asset("rpm", assets), "steelmc-1.0.0-1.noarch.rpm")
        self.assertEqual(up.pick_asset("arch", assets), "steelmc-1.0.0-1-any.pkg.tar.zst")
        with self.assertRaises(up.UpdateError):
            up.pick_asset("deb", {"SHA256SUMS": "u"})

    def test_downloads_are_checked(self):
        good = b"package bytes"
        sums = f"{hashlib.sha256(good).hexdigest()}  steelmc_9.9.9_all.deb\n".encode()
        assets = {"steelmc_9.9.9_all.deb": "pkg", "SHA256SUMS": "sums"}
        with tempfile.TemporaryDirectory() as folder:
            path = up.download("steelmc_9.9.9_all.deb", assets, folder, lambda url, accept=None: {"sums": sums, "pkg": good}[url])
            with open(path, "rb") as f:
                self.assertEqual(f.read(), good)
            with self.assertRaises(up.UpdateError):
                up.download("steelmc_9.9.9_all.deb", assets, folder, lambda url, accept=None: {"sums": sums, "pkg": b"tampered"}[url])
            with self.assertRaises(up.UpdateError):
                up.download("steelmc_9.9.9_all.deb", {"steelmc_9.9.9_all.deb": "pkg"}, folder, lambda url, accept=None: good)

    def test_a_package_install_uses_the_package_manager(self):
        good = b"deb"
        sums = f"{hashlib.sha256(good).hexdigest()}  steelmc_9.9.9_all.deb\n".encode()
        urls = {up.API: release(assets=["steelmc_9.9.9_all.deb", "SHA256SUMS"]), "https://example/SHA256SUMS": sums,
                "https://example/steelmc_9.9.9_all.deb": good}
        ran = []

        def run(argv, **kw):
            ran.append(argv)
            out = "steelmc: /usr/lib/steelmc/devices/steelmc_devices.py" if argv[0] == "dpkg" else ""
            return subprocess.CompletedProcess(argv, 0 if argv[0] in ("dpkg", "sudo") else 1, out, "")
        with tempfile.TemporaryDirectory() as root, mock.patch("shutil.which", lambda name: f"/usr/bin/{name}" if name in ("dpkg", "apt-get") else None):
            os.makedirs(os.path.join(root, "devices"))
            with open(os.path.join(root, "devices", "steelmc_devices.py"), "w") as f:
                f.write('__version__ = "1.0.0"\n')
            code = up.update(yes=True, root=root, fetch=lambda url, accept=None: urls[url], run=run, say=lambda *_: None)
        self.assertEqual(code, 0)
        self.assertEqual(ran[-1][:4], ["sudo", "apt-get", "install", "-y"])
        self.assertTrue(ran[-1][4].endswith("steelmc_9.9.9_all.deb"))

    def test_up_to_date_does_nothing(self):
        said = []
        with tempfile.TemporaryDirectory() as prefix:
            root = os.path.join(prefix, "lib", "steelmc")  # what make install makes
            os.makedirs(os.path.join(root, "devices"))
            with open(os.path.join(root, "devices", "steelmc_devices.py"), "w") as f:
                f.write('__version__ = "9.9.9"\n')
            code = up.update(root=root, fetch=lambda url, accept=None: release("9.9.9"),
                             run=lambda argv, **kw: subprocess.CompletedProcess(argv, 1, "", ""), say=said.append)
        self.assertEqual(code, 0)
        self.assertIn("Up to date", said[-1])


class Kinds(unittest.TestCase):
    def kind(self, root, pacman_says=None):
        def run(argv, **kw):
            if argv[0] == "pacman" and pacman_says:
                return subprocess.CompletedProcess(argv, 0, pacman_says, "")
            return subprocess.CompletedProcess(argv, 1, "", "")
        tools = ("pacman",) if pacman_says else ()
        with mock.patch("shutil.which", lambda name: f"/usr/bin/{name}" if name in tools else None):
            return up.install_kind(root, run)

    def test_tells_make_install_from_a_copy_and_the_aur(self):
        with tempfile.TemporaryDirectory() as prefix:
            made = os.path.join(prefix, "lib", "steelmc")
            unpacked = os.path.join(prefix, "Downloads", "steelmc-main")
            for root in (made, unpacked):
                os.makedirs(root)
            self.assertEqual(self.kind(made), "make")
            self.assertEqual(self.kind(unpacked), "copy")
            owned = "/usr/lib/steelmc/devices/steelmc_devices.py is owned by {} 1.0.0-1\n"
            self.assertEqual(self.kind("/usr/lib/steelmc", owned.format("steelmc")), "arch")
            self.assertEqual(self.kind("/usr/lib/steelmc", owned.format("steelmc-git")), "aur-git")

    def test_a_copy_or_steelmc_git_says_how_to_update_instead(self):
        state = tempfile.TemporaryDirectory()
        self.addCleanup(state.cleanup)
        for kind in ("copy", "aur-git"):
            with mock.patch.object(up, "install_kind", lambda root, run, kind=kind: kind), \
                    mock.patch.dict(os.environ, {"XDG_STATE_HOME": state.name}):
                with self.assertRaises(up.UpdateError):
                    up.update(yes=True, fetch=lambda url, accept=None: release(), say=lambda *_: None)
                self.assertIsNone(up.check_for_app(1000, lambda url, accept=None: release()))


class AppCheck(unittest.TestCase):
    def setUp(self):
        self.state = tempfile.TemporaryDirectory()
        self.addCleanup(self.state.cleanup)
        patcher = mock.patch.dict(os.environ, {"XDG_STATE_HOME": self.state.name})
        patcher.start()
        self.addCleanup(patcher.stop)
        prefix = tempfile.TemporaryDirectory()
        self.addCleanup(prefix.cleanup)
        self.root = os.path.join(prefix.name, "lib", "steelmc")  # a make install
        os.makedirs(os.path.join(self.root, "devices"))
        with open(os.path.join(self.root, "devices", "steelmc_devices.py"), "w") as f:
            f.write('__version__ = "1.0.0"\n')
        self.fetches = 0

    def fetch(self, url, accept=None):
        self.fetches += 1
        return release("1.1.0")

    def check(self, now):
        return up.check_for_app(now, self.fetch, self.root, lambda argv, **kw: subprocess.CompletedProcess(argv, 1, "", ""))

    def test_offers_a_newer_release_once_a_day_at_most(self):
        self.assertEqual(self.check(1000), "1.1.0")
        self.assertEqual(self.check(2000), "1.1.0")
        self.assertEqual(self.fetches, 1)  # the second look used what the first found
        self.check(1000 + 86400)
        self.assertEqual(self.fetches, 2)

    def test_not_now_hides_it_until_the_next_version(self):
        self.assertEqual(self.check(1000), "1.1.0")
        up.dismiss("1.1.0")
        self.assertIsNone(self.check(2000))

    def test_offline_and_off_are_silent(self):
        def offline(url, accept=None):
            raise up.UpdateError("no network")
        self.assertIsNone(up.check_for_app(1000, offline, self.root, lambda argv, **kw: subprocess.CompletedProcess(argv, 1, "", "")))
        with mock.patch.dict(os.environ, {"STEELMC_NO_UPDATE_CHECK": "1"}):
            self.assertIsNone(self.check(5000))


if __name__ == "__main__":
    unittest.main()
