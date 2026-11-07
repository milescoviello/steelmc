# Install steelmc system-wide, the way a distro package would: sudo make install
# Packagers: make DESTDIR="$pkgdir" PREFIX=/usr install
#
# Everything lives in $(LIBDIR), laid out as in this repo, with commands linked into $(BINDIR). The udev rules let the
# logged-in user reach the devices without sudo; uinput is loaded at boot for apex-analog's virtual controllers.

PREFIX  ?= /usr/local
DESTDIR ?=
BINDIR  ?= $(PREFIX)/bin
LIBDIR  ?= $(PREFIX)/lib/steelmc
APPDIR  ?= $(PREFIX)/share/applications
DOCDIR  ?= $(PREFIX)/share/doc/steelmc
LICDIR  ?= $(PREFIX)/share/licenses/steelmc
# udev and modules-load only read /usr/lib and /etc, so a /usr/local install puts these in /etc.
ifeq ($(PREFIX),/usr)
UDEVDIR    ?= /usr/lib/udev/rules.d
MODLOADDIR ?= /usr/lib/modules-load.d
else
UDEVDIR    ?= /etc/udev/rules.d
MODLOADDIR ?= /etc/modules-load.d
endif

RULES = aerox/udev/70-aerox.rules arctis/udev/70-arctis.rules devices/udev/70-steelmc-devices.rules \
        apex-analog/udev/70-apex-analog.rules apex-oled/udev/99-steelseries-apex-oled.rules
# command name -> file under $(LIBDIR)
COMMANDS = steelmc:gui/gui.py steelmc-devices:devices/steelmc_devices.py aerox:aerox/aerox.py arctis:arctis/arctis.py \
           apex-analog:apex-analog/apex_analog.py apex-oled:apex-oled/apex-oled apex-oledd:apex-oled/apex-oledd \
           apex-oledd-supervise:apex-oled/apex-oledd-supervise steelmc-update:update/steelmc_update.py

.PHONY: install uninstall test

install:
	install -d "$(DESTDIR)$(LIBDIR)" "$(DESTDIR)$(LIBDIR)/devices" "$(DESTDIR)$(LIBDIR)/gui" "$(DESTDIR)$(LIBDIR)/aerox" "$(DESTDIR)$(LIBDIR)/arctis" "$(DESTDIR)$(LIBDIR)/apex-analog" \
	           "$(DESTDIR)$(LIBDIR)/apex-oled" "$(DESTDIR)$(LIBDIR)/devices/families" "$(DESTDIR)$(LIBDIR)/update"
	install -m 0755 update/steelmc_update.py "$(DESTDIR)$(LIBDIR)/update/"
	install -m 0755 gui/gui.py "$(DESTDIR)$(LIBDIR)/gui/"
	install -m 0755 aerox/aerox.py "$(DESTDIR)$(LIBDIR)/aerox/"
	install -m 0755 arctis/arctis.py "$(DESTDIR)$(LIBDIR)/arctis/"
	install -m 0755 apex-analog/apex_analog.py "$(DESTDIR)$(LIBDIR)/apex-analog/"
	install -m 0755 apex-oled/apex-oled apex-oled/apex-oledd apex-oled/apex-oledd-supervise "$(DESTDIR)$(LIBDIR)/apex-oled/"
	install -m 0644 apex-oled/config.example.toml apex-oled/apex-oledd.desktop "$(DESTDIR)$(LIBDIR)/apex-oled/"
	install -m 0755 devices/steelmc_devices.py "$(DESTDIR)$(LIBDIR)/devices/"
	install -m 0644 devices/gear.py devices/catalog.json "$(DESTDIR)$(LIBDIR)/devices/"
	install -m 0644 devices/families/*.json "$(DESTDIR)$(LIBDIR)/devices/families/"
	install -d "$(DESTDIR)$(BINDIR)"
	for pair in $(COMMANDS); do ln -sfn "$(LIBDIR)/$${pair#*:}" "$(DESTDIR)$(BINDIR)/$${pair%%:*}"; done
	install -d "$(DESTDIR)$(UDEVDIR)" "$(DESTDIR)$(MODLOADDIR)" "$(DESTDIR)$(APPDIR)" "$(DESTDIR)$(DOCDIR)" "$(DESTDIR)$(LICDIR)"
	install -m 0644 $(RULES) "$(DESTDIR)$(UDEVDIR)/"
	echo uinput > "$(DESTDIR)$(MODLOADDIR)/steelmc.conf"
	sed "s|^Exec=.*|Exec=$(BINDIR)/steelmc|" gui/net.local.steelmc.desktop > "$(DESTDIR)$(APPDIR)/net.local.steelmc.desktop"
	install -m 0644 README.md CHANGELOG.md "$(DESTDIR)$(DOCDIR)/" 2>/dev/null || install -m 0644 README.md "$(DESTDIR)$(DOCDIR)/"
	install -m 0644 LICENSE "$(DESTDIR)$(LICDIR)/"
	@if [ -z "$(DESTDIR)" ]; then \
	    udevadm control --reload-rules 2>/dev/null; \
	    udevadm trigger --subsystem-match=hidraw --subsystem-match=input --subsystem-match=misc 2>/dev/null; \
	    modprobe uinput 2>/dev/null; \
	    echo "Installed. Replug your SteelSeries devices if they don't open straight away."; \
	fi; true

uninstall:
	for pair in $(COMMANDS); do rm -f "$(DESTDIR)$(BINDIR)/$${pair%%:*}"; done
	rm -rf "$(DESTDIR)$(LIBDIR)" "$(DESTDIR)$(DOCDIR)" "$(DESTDIR)$(LICDIR)"
	for rule in $(RULES); do rm -f "$(DESTDIR)$(UDEVDIR)/$${rule##*/}"; done
	rm -f "$(DESTDIR)$(MODLOADDIR)/steelmc.conf" "$(DESTDIR)$(APPDIR)/net.local.steelmc.desktop"

test:
	cd update && python3 -m unittest -q
	cd aerox && python3 -m unittest -q
	cd arctis && python3 -m unittest -q
	cd devices && python3 -m unittest -q
	cd apex-analog && python3 -m unittest -q
