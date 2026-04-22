# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A two-tool package for Linux SocketCAN admins:

- **`canconf`** — one-shot reconfigurator for CAN / CAN-FD interfaces. Replaces the long, order-sensitive `ip link set ...` dance with a single terse command like `canconf 500k/2M@0.875/0.75`. Its niche: hosts with two or more CAN interfaces on the same bus that must be configured identically.
- **`canmon`** — read-only live monitor. Tails state, bittiming, frame-error deltas, and controller bit-error deltas per interface per tick; flags state transitions, config changes, auto-restarts, and threshold breaches.

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

Three modules under `src/canconf/`:

- `common.py` — `discover_ifaces()` (scans `/sys/class/net/*/type` for ARPHRD_CAN = 280), `fmt_rate()` (500000→"500k"), `get_links(stats=False)` (wraps `ip -j -details [-s] link show`, returns `{ifname: dict}`). Shared by both tools.
- `cli.py` — `canconf` entry point.
- `monitor.py` — `canmon` entry point.

`__init__.py` exports `__version__`; `__main__.py` makes `python -m canconf` run the reconfigurator. `canmon` is exposed only via the pyproject entry point (no `python -m canmon`).

**canconf pipeline per run:**

1. **Discover** CAN interfaces by scanning `/sys/class/net/*/type` for ARPHRD_CAN (`280`). This catches `can*`, `vcan*`, `slcan*` without pattern-matching names. Override with `--ifaces`.

2. **Parse** the positional spec. Grammar: `BITRATE[/DBITRATE][@SP[/DSP]]` — a `/` in the rate part switches the action to CAN-FD; an `@` starts sample points. Special specs: `off` / `down`, `up`.

3. **Elevate** via `os.execvp("sudo", [...sys.executable, "-m", "canconf", ...sys.argv[1:]])` if not root. Using `-m canconf` keeps pipx-installed venvs correct (no accidental system-python re-exec).

4. **Apply**: for each selected iface, `ip link set IF down` → `ip link set IF type can ...` (+ `txqueuelen`) → `ip link set IF up`. Default `txqueuelen` is 10000 because the kernel default of 10 is far too low for CAN.

5. **Show** post-apply state unless `--quiet` or `--dry-run`. Status rows include `qlen` (`txqlen` from `ip`) and `drv` (`bittiming_const.name` — typically the SocketCAN driver name). The driver may silently round the requested bitrate to the nearest achievable value given the CAN clock, so reading back the actual config matters.

**Introspection (`canconf bitrates`):** derives the achievable bitrate envelope per interface from `bittiming_const` + `clock` using `bitrate = clock / (brp · (1 + tseg1 + tseg2))` — min comes from the max values, max from the mins. Intersects with the set of standard CAN bitrates (10k…1M for nominal, 1M…8M for FD data) and shows both the raw envelope and the matching standard rates. FD support is detected by presence of `data_bittiming_const`.

**canmon pipeline per tick:**

1. Call `get_links(stats=True)` and wrap each selected iface in a `Snapshot` dataclass (state, bittiming, rx/tx errors from `stats64`, all of `info_xstats`).
2. Compare against the previous tick: detect state transition, bittiming change, restart-count increase, and compute `Δerr/s` and `Δbus/s`.
3. At startup: emit the header and one row per iface (initial snapshot). Subsequent ticks: emit a row only for ifaces where *something* changed (state/bittiming/restart count) or the `Δbus/s` threshold was crossed. `--verbose` overrides this and emits every iface every tick. `--once` prints only the startup snapshot and exits.

**Key design choices:**

- No pattern-matching on interface names — rely on ARPHRD_CAN so exotic transports (slcan, usb-based gs_can, …) are picked up automatically.
- Status is read from `ip -j -details link show` and pulled from `linkinfo.info_data.bittiming` / `.data_bittiming`. CAN-FD is inferred by the presence of `data_bittiming` (not by `ctrlmode_supported`, which only indicates driver capability).
- `ip` is shelled out rather than talking netlink directly: keeps the dep surface at zero and the commands are trivially auditable via `-v`/`-n`.
- Sample-point syntax (`@0.875/0.75`) is opt-in; many users don't care and the kernel picks reasonable defaults.
- `canmon` measures bit-errors via `info_xstats.bus_error` (monotonic counter maintained by the driver), **not** via the live `berr-counter` TEC/REC (which oscillates and resets). This makes delta-per-second meaningful and makes the tool work without `berr-reporting on` at configure time.
- `canmon` does not open a CAN socket; it only polls `ip`/sysfs. Zero bus impact, zero need for root.

## Release

1. Bump `version` in `pyproject.toml` **and** `__version__` in `src/canconf/__init__.py` (both must match — `make release` reads pyproject.toml, but the `--version` output reads `__init__.py`).
2. Commit the bump.
3. `make release` — verifies clean tree, creates `vX.Y.Z` tag, pushes.
4. `make publish` — builds wheel + sdist, 5s confirm prompt, `twine upload`.

## Dependencies

None at runtime. Build requires `hatchling`; `make publish` needs `twine` on PATH; `make fmt`/`lint` use `ruff` if installed (otherwise skipped).
