"""
canmon - monitor SocketCAN / CAN-FD interface health.

Usage:
  canmon                           monitor all can* interfaces, 1 Hz, forever
  canmon -i can0,can1              restrict to these interfaces
  canmon -r 0.5                    tick every 500 ms
  canmon -t 10                     flag lines with > 10 bus-errors / sec
  canmon -o                        single tick, then exit
  canmon -l                        events only (no periodic table lines)

Columns:
  TIME STATE BITRATE  Δerr/s Δbus/s restarts notes

  Δerr/s   frame-level rx+tx errors per second (driver stats64)
  Δbus/s   CAN controller bit-errors per second (info_xstats.bus_error)
  notes    state transitions, config changes, auto-restarts, threshold flags

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
from .common import discover_ifaces, fmt_rate, get_links


@dataclass
class Snapshot:
    state: str
    bitrate: int | None
    dbitrate: int | None
    sample_point: str | None
    dsample_point: str | None
    rx_errors: int
    tx_errors: int
    bus_error: int
    arbitration_lost: int
    restarts: int
    error_warning: int
    error_passive: int
    bus_off: int

    @classmethod
    def from_link(cls, link: dict) -> "Snapshot":
        li = link.get("linkinfo") or {}
        info_data = li.get("info_data") or {}
        xstats = li.get("info_xstats") or {}
        bt = info_data.get("bittiming") or {}
        dbt = info_data.get("data_bittiming") or {}
        stats = link.get("stats64") or {}
        rx = stats.get("rx") or {}
        tx = stats.get("tx") or {}
        return cls(
            state=info_data.get("state", "?"),
            bitrate=bt.get("bitrate"),
            dbitrate=dbt.get("bitrate"),
            sample_point=bt.get("sample_point"),
            dsample_point=dbt.get("sample_point"),
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
            return "-"
        r = fmt_rate(self.bitrate)
        if self.dbitrate:
            r += f"/{fmt_rate(self.dbitrate)}"
        return r

    def bittiming_key(self) -> tuple:
        return (self.bitrate, self.dbitrate, self.sample_point, self.dsample_point)


def snapshot_all(ifaces: list[str]) -> dict[str, Snapshot]:
    links = get_links(stats=True)
    return {i: Snapshot.from_link(links.get(i, {})) for i in ifaces}


HEADER = f"{'TIME':>8}  {'IFACE':<6}  {'STATE':<14}  {'BITRATE':<10}  {'Δerr/s':>6}  {'Δbus/s':>6}  {'restarts':>8}  notes"


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
    ap.add_argument("-o", "--once", action="store_true")
    ap.add_argument("-l", "--log-only", action="store_true",
                    help="print events only, no periodic table lines")
    ap.add_argument("-V", "--version", action="store_true")
    ap.add_argument("-h", "--help", action="store_true")
    args = ap.parse_args()

    if args.help:
        print(__doc__.strip())
        return 0
    if args.version:
        print(f"canmon {__version__}")
        return 0

    if args.ifaces:
        ifaces = [x.strip() for x in args.ifaces.split(",") if x.strip()]
    else:
        ifaces = discover_ifaces()
    if not ifaces:
        print("canmon: no CAN interfaces found (pass --ifaces to override)", file=sys.stderr)
        return 1

    signal.signal(signal.SIGINT, lambda *_: (print(), sys.exit(0)))

    prev = snapshot_all(ifaces)
    if not args.log_only:
        print(HEADER)

    while True:
        time.sleep(args.rate)
        now = snapshot_all(ifaces)
        ts = datetime.now().strftime("%H:%M:%S")
        for iface in ifaces:
            p = prev[iface]
            n = now[iface]

            notes = []
            if p.state != n.state:
                notes.append(f"STATE {p.state}→{n.state}")
            if p.bittiming_key() != n.bittiming_key():
                notes.append(f"CONFIG {p.rate_str()}→{n.rate_str()}")
            if n.restarts > p.restarts:
                notes.append(f"RESTART #{n.restarts}")

            d_err = (n.rx_errors - p.rx_errors) + (n.tx_errors - p.tx_errors)
            d_bus = n.bus_error - p.bus_error
            err_per_s = d_err / args.rate
            bus_per_s = d_bus / args.rate

            flagged = bus_per_s > args.err_rate
            if flagged:
                notes.append(f"BIT-ERRORS {bus_per_s:.0f}/s > {args.err_rate:.0f}/s")

            flag = "!" if flagged else " "
            note_str = "  ".join(notes)

            if args.log_only:
                if notes:
                    print(f"{ts}  {iface:<6}  {flag} {note_str}")
            else:
                print(
                    f"{ts}  {iface:<6}  {n.state:<14}  {n.rate_str():<10}  "
                    f"{err_per_s:>6.0f}  {bus_per_s:>6.0f}  {n.restarts:>8}  "
                    f"{flag} {note_str}".rstrip()
                )

        prev = now
        if args.once:
            return 0


if __name__ == "__main__":
    sys.exit(main())
