PKG       := canconf
PYTHON    ?= python3
PIPX      ?= pipx
TWINE     ?= twine

# For running from a source checkout without installing.
RUN_PY    := PYTHONPATH=src $(PYTHON)

PREFIX    ?= $(HOME)/.local
MANDIR    ?= $(PREFIX)/share/man

VERSION   := $(shell grep '^version' pyproject.toml | head -1 | sed 's/.*"\(.*\)"/\1/')

.PHONY: all help build install install-editable uninstall reinstall \
        run status monitor talk dry test check fmt lint clean distclean \
        vcan vcanfd vcan-down man man-canmon man-cantalk release publish

all: help

# --- build / install ---------------------------------------------------------

build:
	$(PYTHON) -m build

install:
	$(PIPX) install --force .
	install -Dm644 man/canconf.1 $(MANDIR)/man1/canconf.1
	install -Dm644 man/canmon.1  $(MANDIR)/man1/canmon.1
	install -Dm644 man/cantalk.1 $(MANDIR)/man1/cantalk.1

install-editable:
	$(PIPX) install --force --editable .

uninstall:
	-$(PIPX) uninstall $(PKG)
	rm -f $(MANDIR)/man1/canconf.1 $(MANDIR)/man1/canmon.1 $(MANDIR)/man1/cantalk.1

reinstall: uninstall install

# --- run / test --------------------------------------------------------------

# Show current status of all CAN interfaces on this host.
status:
	$(RUN_PY) -m $(PKG)

# Monitor all CAN interfaces from a source checkout.
monitor:
	$(RUN_PY) -c "from canconf.monitor import main; import sys; sys.exit(main())"

# Open the cantalk REPL from a source checkout. Override IFACE / ARB / RAW.
# Example: make talk IFACE=vcan0 ARB=7DF,7E8
# Example: make talk IFACE=vcan0 ARB=7DF RAW=1
IFACE ?=
ARB   ?=
RAW   ?=
talk:
	$(RUN_PY) -m canconf.cantalk $(IFACE) $(ARB) $(if $(RAW),--raw)

# Dry-run a full reconfigure; override SPEC / IFACES on the command line.
SPEC   ?= 500k/2M@0.875/0.75
IFACES ?=
dry:
	$(RUN_PY) -m $(PKG) -n $(SPEC) $(if $(IFACES),-i $(IFACES))

# Quick sanity checks: parse help, parse a handful of specs.
test:
	$(RUN_PY) -m $(PKG) --help >/dev/null
	$(RUN_PY) -m $(PKG) -n 500k -i vcan0 >/dev/null
	$(RUN_PY) -m $(PKG) -n 500k/2M -i vcan0 >/dev/null
	$(RUN_PY) -m $(PKG) -n 500k/2M@0.875/0.75 -i vcan0 >/dev/null
	$(RUN_PY) -m $(PKG) -n off -i vcan0 >/dev/null
	$(RUN_PY) -c "from canconf.monitor import main; import sys; sys.argv=['canmon','--help']; sys.exit(main())" >/dev/null
	$(RUN_PY) -c "from canconf.monitor import main; import sys; sys.argv=['canmon','--once','-i','vcan0']; sys.exit(main())" >/dev/null
	$(RUN_PY) -m canconf.cantalk --help >/dev/null
	$(RUN_PY) -m canconf.cantalk --version >/dev/null
	$(RUN_PY) -c "from canconf import cantalk as t; \
		assert t.parse_hex('0902') == bytes([9,2]); \
		assert t.parse_id('7DF') == 0x7DF; \
		assert t.derive_rx(0x7E0) == 0x7E8; \
		assert t.derive_rx(0x18DA10F1) == 0x18DAF110"
	@echo "ok"

# --- quality -----------------------------------------------------------------

check:
	$(PYTHON) -m compileall -q src

fmt:
	@command -v ruff >/dev/null && ruff format src || echo "ruff not installed, skipping"

lint:
	@command -v ruff >/dev/null && ruff check src || echo "ruff not installed, skipping"

# --- virtual CAN for local testing -------------------------------------------

vcan:
	sudo modprobe vcan
	sudo ip link add dev vcan0 type vcan 2>/dev/null || true
	sudo ip link set up vcan0

# vcan with CAN-FD MTU so canconf can test FD code paths.
vcanfd:
	sudo modprobe vcan
	sudo ip link add dev vcan0 type vcan mtu 72 2>/dev/null || true
	sudo ip link set up vcan0

vcan-down:
	-sudo ip link set down vcan0
	-sudo ip link delete vcan0

# --- docs --------------------------------------------------------------------

man:
	@man ./man/canconf.1

man-canmon:
	@man ./man/canmon.1

man-cantalk:
	@man ./man/cantalk.1

# --- release -----------------------------------------------------------------

# Tag the current HEAD with the version from pyproject.toml and push.
release:
	@if ! git diff --quiet || ! git diff --cached --quiet; then \
		echo "error: uncommitted changes — commit first"; exit 1; \
	fi
	@if git tag | grep -q "^v$(VERSION)$$"; then \
		echo "error: tag v$(VERSION) already exists — bump version in pyproject.toml and src/$(PKG)/__init__.py"; exit 1; \
	fi
	git tag -a v$(VERSION) -m "v$(VERSION)"
	git push --tags
	@echo "Tagged and pushed v$(VERSION)."
	@echo "Run 'make publish' to upload to PyPI."

# Build and upload to PyPI. Requires twine and credentials (e.g. ~/.pypirc).
publish: clean build
	@echo ""
	@echo "About to upload $(PKG) $(VERSION) to PyPI in 5s... (Ctrl-C to abort)"
	@sleep 5
	$(TWINE) upload --skip-existing dist/*

# --- housekeeping ------------------------------------------------------------

clean:
	rm -rf build dist *.egg-info src/*.egg-info .pytest_cache
	find . -name __pycache__ -type d -exec rm -rf {} +

distclean: clean
	rm -rf .venv

help:
	@echo "$(PKG) Makefile (v$(VERSION))"
	@echo ""
	@echo "Build / install:"
	@echo "  build              python -m build (wheel + sdist in dist/)"
	@echo "  install            pipx install --force . + install man page"
	@echo "  install-editable   pipx install --editable ."
	@echo "  uninstall          pipx uninstall + remove man page"
	@echo "  reinstall          uninstall + install"
	@echo ""
	@echo "Run / test:"
	@echo "  status             show current CAN interface status (canconf)"
	@echo "  monitor            live CAN health monitor (canmon)"
	@echo "  talk IFACE=can0    open the cantalk REPL on IFACE (ARB=7DF,7E8 RAW=1 optional)"
	@echo "  dry SPEC=500k/2M   dry-run a reconfigure"
	@echo "  test               run quick self-tests"
	@echo "  check              python -m compileall"
	@echo "  fmt / lint         ruff format / ruff check (if installed)"
	@echo ""
	@echo "Virtual CAN:"
	@echo "  vcan               create vcan0 (classic CAN)"
	@echo "  vcanfd             create vcan0 with CAN-FD MTU"
	@echo "  vcan-down          tear down vcan0"
	@echo ""
	@echo "Docs:"
	@echo "  man                view canconf(1)"
	@echo "  man-canmon         view canmon(1)"
	@echo "  man-cantalk        view cantalk(1)"
	@echo ""
	@echo "Release:"
	@echo "  release            tag v\$$VERSION and push"
	@echo "  publish            build + twine upload"
	@echo ""
	@echo "Housekeeping:"
	@echo "  clean / distclean  remove build artifacts / .venv"
	@echo ""
	@echo "Variables: PYTHON=$(PYTHON)  PIPX=$(PIPX)  PREFIX=$(PREFIX)"
