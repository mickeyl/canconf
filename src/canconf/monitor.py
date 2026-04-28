"""
canmon - monitor SocketCAN / CAN-FD interface health.

Usage:
  canmon                           monitor all can* interfaces, 1 Hz, forever
  canmon -i can0,can1              restrict to these interfaces
  canmon -r 0.5                    tick every 500 ms
  canmon -t 10                     flag ticks with > 10 bus-errors / sec
  canmon -o                        snapshot the current state and exit
  canmon -v                        verbose: emit a row every tick

Default behaviour: print the current state of every selected interface once
at startup, keep discovering CAN interfaces that appear later, then stay
silent until something changes. A per-interface row is emitted only on a state
transition, bittiming change, auto-restart, or when the bit-error rate exceeds
the --err-rate threshold.

Columns:
  TIME STATE BITRATE  Δerr/s Δbus/s restarts notes

  Δerr/s   frame-level rx+tx errors per second (driver stats64)
  Δbus/s   CAN controller bit-errors per second (info_xstats.bus_error)
  notes    state transitions, config changes, auto-restarts, threshold flags

  STATE may be MISSING when a selected iface is absent from `ip link show`, or
  NO-CAN-DATA when the link exists but `ip` does not expose CAN details.

Does not require root — it only reads kernel stats.
"""
from __future__ import annotations

import argparse
import signal
import sys
import time
from dataclasses import dataclass
from datetime import datetime

from . import __version__
from . import common
from .common import (
    BOLD, BRIGHT_RED, CYAN, DIM, MAGENTA, MISSING_VALUE, YELLOW,
    c, color_state, discover_ifaces, fmt_rate, get_links,
)

HEADER_TEXT = (
    f"{'TIME':>8}  {'IFACE':<6}  {'STATE':<14}  {'BITRATE':<10}  "
    f"{'Δerr/s':>6}  {'Δbus/s':>6}  {'restarts':>8}  notes"
)


@dataclass
class Snapshot:
    present: bool
    can_data: bool
    state: str
    bitrate: int | None
    dbitrate: int | None
    sample_point: str | None
    dsample_point: str | None
    txqlen: int | None
    driver: str | None
    rx_errors: int
    tx_errors: int
    bus_error: int
    arbitration_lost: int
    restarts: int
    error_warning: int
    error_passive: int
    bus_off: int

    @classmethod
    def from_link(cls, link: dict | None) -> "Snapshot":
        if link is None:
            return cls(
                present=False,
                can_data=False,
                state="MISSING",
                bitrate=None,
                dbitrate=None,
                sample_point=None,
                dsample_point=None,
                txqlen=None,
                driver=None,
                rx_errors=0,
                tx_errors=0,
                bus_error=0,
                arbitration_lost=0,
                restarts=0,
                error_warning=0,
                error_passive=0,
                bus_off=0,
            )

        li = link.get("linkinfo") or {}
        info_data = li.get("info_data") or {}
        xstats = li.get("info_xstats") or {}
        bt = info_data.get("bittiming") or {}
        dbt = info_data.get("data_bittiming") or {}
        stats = link.get("stats64") or {}
        rx = stats.get("rx") or {}
        tx = stats.get("tx") or {}
        can_data = "state" in info_data
        return cls(
            present=True,
            can_data=can_data,
            state=info_data.get("state", "NO-CAN-DATA"),
            bitrate=bt.get("bitrate"),
            dbitrate=dbt.get("bitrate"),
            sample_point=bt.get("sample_point"),
            dsample_point=dbt.get("sample_point"),
            txqlen=link.get("txqlen"),
            driver=(info_data.get("bittiming_const") or {}).get("name"),
            rx_errors=rx.get("errors", 0),
            tx_errors=tx.get("errors", 0),
            bus_error=xstats.get("bus_error", 0),
            arbitration_lost=xstats.get("arbitration_lost", 0),
            restarts=xstats.get("restarts", 0),
            error_warning=xstats.get("error_warning", 0),
            error_passive=xstats.get("error_passive", 0),
            bus_off=xstats.get("bus_off", 0),
        )

    def rate_str(self) -> str:
        if self.bitrate is None:
            return MISSING_VALUE
        r = fmt_rate(self.bitrate)
        if self.dbitrate:
            r += f"/{fmt_rate(self.dbitrate)}"
        return r

    def bittiming_key(self) -> tuple:
        return (self.bitrate, self.dbitrate, self.sample_point, self.dsample_point)


def snapshot_all(ifaces: list[str]) -> dict[str, Snapshot]:
    links = get_links(stats=True)
    return {i: Snapshot.from_link(links.get(i)) for i in ifaces}


def add_discovered_ifaces(ifaces: list[str], prev: dict[str, Snapshot]) -> list[str]:
    """Add newly discovered CAN interfaces without forgetting vanished ones."""
    seen = set(ifaces)
    for iface in discover_ifaces():
        if iface not in seen:
            ifaces.append(iface)
            prev[iface] = Snapshot.from_link(None)
            seen.add(iface)
    return sorted(ifaces)


def header() -> str:
    return f"{c(HEADER_TEXT, BOLD)}\n{separator()}"


def separator(label: str | None = None) -> str:
    width = len(HEADER_TEXT)
    if not label:
        return c("-" * width, DIM)
    pad = f"  {label}  "
    if len(pad) + 6 > width:
        return c("-" * width + "\n" + label, DIM)
    side = (width - len(pad)) // 2
    left = "-" * side
    right = "-" * (width - side - len(pad))
    return f"{c(left, DIM)}{c(pad, BOLD)}{c(right, DIM)}"


def color_note(note: str) -> str:
    if note.startswith("STATE "):
        tail = note[len("STATE "):]
        arrow = tail.find(" → ")
        if arrow != -1:
            old, new = tail[:arrow], tail[arrow + 3:]
            return f"STATE {color_state(old)} → {color_state(new)}"
        return c(note, MAGENTA)
    if note.startswith("CONFIG "):
        return c(note, MAGENTA)
    if note.startswith("RESTART "):
        return c(note, CYAN, BOLD)
    if note.startswith("BIT-ERRORS "):
        return c(note, BRIGHT_RED, BOLD)
    return note


def color_rate_delta(per_s: float, flagged: bool) -> str:
    cell = f"{per_s:>6.0f}"
    if flagged:
        return c(cell, BRIGHT_RED, BOLD)
    if per_s > 0:
        return c(cell, YELLOW)
    return cell


def counter_rates(p: Snapshot, n: Snapshot, interval: float) -> tuple[float, float]:
    if not p.present or not n.present:
        return 0.0, 0.0
    d_err = (n.rx_errors - p.rx_errors) + (n.tx_errors - p.tx_errors)
    d_bus = n.bus_error - p.bus_error
    if d_err < 0 or d_bus < 0:
        return 0.0, 0.0
    return d_err / interval, d_bus / interval


def initial_notes(n: Snapshot) -> list[str]:
    notes = []
    if n.sample_point and n.dsample_point:
        notes.append(f"sp {n.sample_point}/{n.dsample_point}")
    elif n.sample_point:
        notes.append(f"sp {n.sample_point}")
    if n.txqlen is not None:
        notes.append(f"qlen {n.txqlen}")
    if n.driver:
        notes.append(f"drv {n.driver}")
    return notes


def fmt_row(ts: str, iface: str, n: Snapshot, err_per_s: float,
            bus_per_s: float, flagged: bool, notes: list[str]) -> str:
    flag = c("!", BRIGHT_RED, BOLD) if flagged else " "
    note_str = "  ".join(color_note(x) for x in notes)
    err_cell = color_rate_delta(err_per_s, err_per_s > 0 and not flagged)
    bus_cell = color_rate_delta(bus_per_s, flagged)
    restarts_cell = f"{n.restarts:>8}"
    if n.restarts > 0:
        restarts_cell = c(restarts_cell, CYAN)
    return (
        f"{c(ts, DIM)}  {iface:<6}  {color_state(n.state, 14)}  {n.rate_str():<10}  "
        f"{err_cell}  {bus_cell}  {restarts_cell}  "
        f"{flag} {note_str}".rstrip()
    )


def main() -> int:
    ap = argparse.ArgumentParser(
        prog="canmon", add_help=False,
        description="Monitor SocketCAN / CAN-FD state, configuration, and bit-error rate.",
    )
    ap.add_argument("-i", "--ifaces")
    ap.add_argument("-r", "--rate", type=float, default=1.0,
                    help="tick interval in seconds (default: 1.0)")
    ap.add_argument("-t", "--err-rate", type=float, default=1.0,
                    help="flag ticks when bus-errors/sec exceeds this (default: 1)")
    ap.add_argument("-o", "--once", action="store_true",
                    help="print the initial snapshot and exit")
    ap.add_argument("-v", "--verbose", action="store_true",
                    help="emit a row every tick, not only on change")
    ap.add_argument("--no-color", action="store_true", help="disable ANSI colour")
    ap.add_argument("-V", "--version", action="store_true")
    ap.add_argument("-h", "--help", action="store_true")
    args = ap.parse_args()

    if args.no_color:
        common.set_color(False)

    if args.help:
        print(__doc__.strip())
        return 0
    if args.version:
        print(f"canmon {__version__}")
        return 0

    auto_discover = not args.ifaces
    if args.ifaces:
        ifaces = [x.strip() for x in args.ifaces.split(",") if x.strip()]
    else:
        ifaces = discover_ifaces()
    if not ifaces and args.once:
        print("canmon: no CAN interfaces found (pass --ifaces to override)", file=sys.stderr)
        return 1

    signal.signal(signal.SIGINT, lambda *_: (print(), sys.exit(0)))

    # Initial snapshot: always printed, so the user sees what is being monitored.
    prev = snapshot_all(ifaces)
    print(header())
    now_dt = datetime.now()
    ts = now_dt.strftime("%H:%M:%S")
    params_emitted: set[str] = set()
    for iface in ifaces:
        print(fmt_row(ts, iface, prev[iface], 0.0, 0.0, flagged=False,
                      notes=initial_notes(prev[iface])))
        if prev[iface].present:
            params_emitted.add(iface)
    last_output_date = now_dt.date() if ifaces else None
    if not ifaces:
        print("canmon: no CAN interfaces found yet; waiting...", file=sys.stderr)
    sys.stdout.flush()

    if args.once:
        return 0

    while True:
        time.sleep(args.rate)
        if auto_discover:
            ifaces = add_discovered_ifaces(ifaces, prev)
        now = snapshot_all(ifaces)
        now_dt = datetime.now()
        ts = now_dt.strftime("%H:%M:%S")
        today = now_dt.date()
        for iface in ifaces:
            p = prev[iface]
            n = now[iface]

            notes = []
            if p.state != n.state:
                notes.append(f"STATE {p.state} → {n.state}")
            if p.bittiming_key() != n.bittiming_key():
                notes.append(f"CONFIG {p.rate_str()} → {n.rate_str()}")
            if n.present and iface not in params_emitted:
                notes.extend(initial_notes(n))
                params_emitted.add(iface)
            if n.restarts > p.restarts:
                notes.append(f"RESTART #{n.restarts}")

            err_per_s, bus_per_s = counter_rates(p, n, args.rate)

            flagged = bus_per_s > args.err_rate
            if flagged:
                notes.append(f"BIT-ERRORS {bus_per_s:.0f}/s > {args.err_rate:.0f}/s")

            if args.verbose or notes:
                if last_output_date is not None and today != last_output_date:
                    print(separator(f"New Day: {today.strftime('%a %Y-%m-%d')}"))
                print(fmt_row(ts, iface, n, err_per_s, bus_per_s, flagged, notes))
                last_output_date = today

        prev = now
        sys.stdout.flush()


if __name__ == "__main__":
    sys.exit(main())
