# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

`canconf` is a one-shot reconfigurator for SocketCAN / CAN-FD interfaces on Linux. It replaces the long, order-sensitive `ip link set ...` dance you'd otherwise run per interface with a single terse command like `canconf 500k/2M@0.875/0.75`. Its niche: hosts with two or more CAN interfaces on the same bus that must be configured identically.

Pure Python 3.9+ stdlib — no runtime dependencies. Distributed on PyPI, meant to be installed via `pipx`.

## Build and test

```bash
make test                      # quick dry-run self-checks (no root needed)
make status                    # show current CAN interface state
make dry SPEC=500k/2M          # show the ip commands canconf would run
make vcan                      # create vcan0 for local testing
make vcanfd                    # vcan0 with CAN-FD MTU
make check                     # python -m compileall
make fmt / make lint           # ruff (if installed)
make build                     # python -m build -> dist/
make install                   # pipx install --force . + man page
make install-editable          # pipx install --editable .
```

From a source checkout, run the tool without installing via `PYTHONPATH=src python -m canconf ...` (this is what `make status`/`make dry` do).

Testing against real hardware needs root; `canconf` self-elevates via `sudo -- python -m canconf ...` when not already root. Dry-run (`-n`) never elevates.

## Architecture

Single-module package. Everything lives in `src/canconf/cli.py` (~200 lines). `__init__.py` exports `__version__`; `__main__.py` makes `python -m canconf` work.

**Pipeline per run:**

1. **Discover** CAN interfaces by scanning `/sys/class/net/*/type` for ARPHRD_CAN (`280`). This catches `can*`, `vcan*`, `slcan*` without pattern-matching names. Override with `--ifaces`.

2. **Parse** the positional spec. Grammar: `BITRATE[/DBITRATE][@SP[/DSP]]` — a `/` in the rate part switches the action to CAN-FD; an `@` starts sample points. Special specs: `off` / `down`, `up`.

3. **Elevate** via `os.execvp("sudo", [...sys.executable, "-m", "canconf", ...sys.argv[1:]])` if not root. Using `-m canconf` keeps pipx-installed venvs correct (no accidental system-python re-exec).

4. **Apply**: for each selected iface, `ip link set IF down` → `ip link set IF type can ...` (+ `txqueuelen`) → `ip link set IF up`. Default `txqueuelen` is 10000 because the kernel default of 10 is far too low for CAN.

5. **Show** post-apply state unless `--quiet` or `--dry-run`. The driver may silently round the requested bitrate to the nearest achievable value given the CAN clock, so reading back the actual config matters.

**Key design choices:**

- No pattern-matching on interface names — rely on ARPHRD_CAN so exotic transports (slcan, usb-based gs_can, …) are picked up automatically.
- Status is read from `ip -j -details link show` and pulled from `linkinfo.info_data.bittiming` / `.data_bittiming`. CAN-FD is inferred by the presence of `data_bittiming` (not by `ctrlmode_supported`, which only indicates driver capability).
- `ip` is shelled out rather than talking netlink directly: keeps the dep surface at zero and the commands are trivially auditable via `-v`/`-n`.
- Sample-point syntax (`@0.875/0.75`) is opt-in; many users don't care and the kernel picks reasonable defaults.

## Release

1. Bump `version` in `pyproject.toml` **and** `__version__` in `src/canconf/__init__.py` (both must match — `make release` reads pyproject.toml, but the `--version` output reads `__init__.py`).
2. Commit the bump.
3. `make release` — verifies clean tree, creates `vX.Y.Z` tag, pushes.
4. `make publish` — builds wheel + sdist, 5s confirm prompt, `twine upload`.

## Dependencies

None at runtime. Build requires `hatchling`; `make publish` needs `twine` on PATH; `make fmt`/`lint` use `ruff` if installed (otherwise skipped).
