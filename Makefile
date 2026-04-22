PKG       := canconf
PYTHON    ?= python3
PIPX      ?= pipx

# For running from a source checkout without installing.
RUN_PY    := PYTHONPATH=src $(PYTHON)

PREFIX    ?= $(HOME)/.local
MANDIR    ?= $(PREFIX)/share/man

VERSION   := $(shell grep '^version' pyproject.toml | head -1 | sed 's/.*"\(.*\)"/\1/')

.PHONY: all help build install install-editable uninstall reinstall \
        run status dry test check fmt lint clean distclean \
        vcan vcanfd vcan-down man release publish

all: help

# --- build / install ---------------------------------------------------------

build:
	$(PYTHON) -m build

install:
	$(PIPX) install --force .
	install -Dm644 man/$(PKG).1 $(MANDIR)/man1/$(PKG).1

install-editable:
	$(PIPX) install --force --editable .

uninstall:
	-$(PIPX) uninstall $(PKG)
	rm -f $(MANDIR)/man1/$(PKG).1

reinstall: uninstall install

# --- run / test --------------------------------------------------------------

# Show current status of all CAN interfaces on this host.
status:
	$(RUN_PY) -m $(PKG)

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
	@man ./man/$(PKG).1

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
	$(PYTHON) -m twine upload --skip-existing dist/*

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
	@echo "  status             show current CAN interface status"
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
	@echo "  man                view man page"
	@echo ""
	@echo "Release:"
	@echo "  release            tag v\$$VERSION and push"
	@echo "  publish            build + twine upload"
	@echo ""
	@echo "Housekeeping:"
	@echo "  clean / distclean  remove build artifacts / .venv"
	@echo ""
	@echo "Variables: PYTHON=$(PYTHON)  PIPX=$(PIPX)  PREFIX=$(PREFIX)"
