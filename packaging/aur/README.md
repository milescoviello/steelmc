# AUR packages

- `steelmc-git` builds the newest commit on main.
- `steelmc` builds a tagged release (set `pkgver`, then `updpkgsums` once the tag exists).

Both run `make DESTDIR="$pkgdir" PREFIX=/usr install` from the top of the repo, then the `steelmc.install` hooks reload udev and load uinput.

To publish or update one (needs an AUR account with your SSH key):

```
git clone ssh://aur@aur.archlinux.org/steelmc-git.git aur-steelmc-git
cp packaging/aur/steelmc-git/PKGBUILD packaging/aur/steelmc-git/steelmc.install aur-steelmc-git/
cd aur-steelmc-git
makepkg --printsrcinfo > .SRCINFO
git add PKGBUILD steelmc.install .SRCINFO
git commit -m "Update"
git push
```
