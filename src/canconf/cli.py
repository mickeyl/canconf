"""
canconf - reconfigure all CAN / CAN-FD interfaces in one shot.

Usage:
  canconf                         show status of all can* interfaces
  canconf 500k                    classic CAN @ 500 kbit/s, up
  canconf 500k/2M                 CAN-FD: nominal 500k, data 2M, up
  canconf 500k/2M@0.8/0.75        same, with nominal/data sample points
  canconf off            | down   bring all interfaces down
  canconf up                      bring all interfaces up (no reconfigure)
  canconf bitrates                show achievable bitrates per interface

  -i, --ifaces a,b,c              only these (default: all can*)
  -r, --restart-ms N              auto-restart on bus-off after N ms
      --listen-only               listen-only mode
      --loopback                  loopback mode
      --one-shot                  one-shot mode
      --berr                      enable bus error reporting
      --term OHM                  set termination resistor (if supported)
      --txqueuelen N              set tx queue length (default: 10000)
  -n, --dry-run                   print ip commands, do not execute
  -v, --verbose                   print ip commands as they run
  -q, --quiet                     suppress post-apply status print
  -V, --version                   print version and exit
  -h, --help

Bitrates: plain int, or K/M suffix (125k, 500k, 1M, 2M, 5M, 8M).
Requires root; self-elevates with sudo if needed.
"""
from __future__ import annotations

import argparse
import os
import shlex
import subprocess
import sys

from . import __version__
from . import common
from .common import (
    BOLD, CYAN, DIM, MAGENTA,
    c, color_state, discover_ifaces, fmt_rate, get_links,
)

DEFAULT_TXQUEUELEN = 10000

# Standard CAN bitrates worth highlighting in `canconf bitrates`.
STD_NOMINAL = [10_000, 20_000, 50_000, 100_000, 125_000, 250_000,
               500_000, 800_000, 1_000_000]
STD_DATA = [1_000_000, 2_000_000, 4_000_000, 5_000_000, 8_000_000]


def parse_rate(s: str) -> int:
    s = s.strip()
    if not s:
        raise ValueError("empty bitrate")
    mult = 1
    if s[-1] in "kK":
        mult, s = 1_000, s[:-1]
    elif s[-1] in "mM":
        mult, s = 1_000_000, s[:-1]
    return int(float(s) * mult)


def parse_spec(spec: str) -> dict:
    """Parse BITRATE[/DBITRATE][@SP[/DSP]] or 'off'/'down'/'up'."""
    if spec in ("off", "down"):
        return {"action": "down"}
    if spec == "up":
        return {"action": "up"}

    rate_part, _, sp_part = spec.partition("@")
    nom_s, sep, data_s = rate_part.partition("/")
    out: dict = {"action": "configure", "bitrate": parse_rate(nom_s), "fd": bool(sep)}
    if sep:
        out["dbitrate"] = parse_rate(data_s)

    if sp_part:
        nsp_s, _, dsp_s = sp_part.partition("/")
        if nsp_s:
            out["sample_point"] = float(nsp_s)
        if dsp_s:
            out["dsample_point"] = float(dsp_s)
    return out


def run(cmd: list[str], *, dry: bool, verbose: bool) -> None:
    if dry or verbose:
        print("+ " + " ".join(shlex.quote(c) for c in cmd))
    if dry:
        return
    r = subprocess.run(cmd)
    if r.returncode != 0:
        sys.exit(r.returncode)


def show_status(ifaces: list[str]) -> None:
    if not ifaces:
        print("no CAN interfaces found")
        return
    links = get_links()

    rows = []
    for name in ifaces:
        link = links.get(name, {})
        state = link.get("operstate", "?")
        qlen = link.get("txqlen")
        data = link.get("linkinfo", {}).get("info_data", {}) or {}
        bt = data.get("bittiming") or {}
        dbt = data.get("data_bittiming")
        driver = (data.get("bittiming_const") or {}).get("name") or "-"
        mode = "CAN-FD" if dbt else "CAN"
        if bt.get("bitrate"):
            rate = fmt_rate(bt["bitrate"])
            if dbt and dbt.get("bitrate"):
                rate += f"/{fmt_rate(dbt['bitrate'])}"
        else:
            rate = "-"
        sp = bt.get("sample_point")
        dsp = (dbt or {}).get("sample_point")
        if sp and dsp:
            sp_col = f"{sp}/{dsp}"
        elif sp:
            sp_col = str(sp)
        else:
            sp_col = "-"
        qlen_col = str(qlen) if qlen is not None else "-"
        rows.append((name, state, mode, rate, sp_col, qlen_col, driver))

    cols = list(zip(*rows))
    w = [max(len(col_val) for col_val in col) for col in cols]
    for name, state, mode, rate, sp_col, qlen_col, driver in rows:
        mode_colored = c(mode, MAGENTA) if mode == "CAN-FD" else c(mode, CYAN)
        # color_state pads internally so w[1] is still the correct visual width
        print(
            f"{c(name, BOLD):<{w[0] + len(c('', BOLD))}}"
            f"  {color_state(state, w[1])}"
            f"  {mode_colored + ' ' * (w[2] - len(mode))}"
            f"  {rate:<{w[3]}}"
            f"  {c('sp', DIM)} {sp_col:<{w[4]}}"
            f"  {c('qlen', DIM)} {qlen_col:<{w[5]}}"
            f"  {c('drv', DIM)} {c(driver, CYAN)}"
        )


def bittiming_range(clock: int, const: dict) -> tuple[int, int]:
    """Return (min_bitrate, max_bitrate) achievable by the given bittiming_const.

    bitrate = clock / (brp * (1 + tseg1 + tseg2))
    """
    brp = const["brp"]
    tseg1 = const["tseg1"]
    tseg2 = const["tseg2"]
    lo = clock // (brp["max"] * (1 + tseg1["max"] + tseg2["max"]))
    hi = clock // (brp["min"] * (1 + tseg1["min"] + tseg2["min"]))
    return lo, hi


def show_bitrates(ifaces: list[str]) -> None:
    links = get_links()
    for name in ifaces:
        link = links.get(name, {})
        data = link.get("linkinfo", {}).get("info_data") or {}
        clock = data.get("clock")
        btc = data.get("bittiming_const")
        dbtc = data.get("data_bittiming_const")

        print(f"=== {name} ===")
        if not clock or not btc:
            print("  (no bittiming constants reported — vcan or similar)")
            print()
            continue

        lo, hi = bittiming_range(clock, btc)
        std = [r for r in STD_NOMINAL if lo <= r <= hi]
        print(f"  driver:    {btc.get('name', '-')}")
        print(f"  clock:     {clock / 1_000_000:g} MHz")
        print(f"  nominal:   {fmt_rate(lo)} .. {fmt_rate(hi)}")
        print(f"  standard:  {', '.join(fmt_rate(r) for r in std) or '(none)'}")

        if dbtc:
            dlo, dhi = bittiming_range(clock, dbtc)
            dstd = [r for r in STD_DATA if dlo <= r <= dhi]
            print(f"  FD data:   {fmt_rate(dlo)} .. {fmt_rate(dhi)}")
            print(f"  FD std:    {', '.join(fmt_rate(r) for r in dstd) or '(none)'}")
        else:
            print(f"  FD:        not supported")
        print()


def elevate_if_needed() -> None:
    if os.geteuid() == 0:
        return
    # Re-exec via `python -m canconf` so it works whether installed via pipx,
    # pip, or run from a source checkout.
    os.execvp("sudo", ["sudo", "--", sys.executable, "-m", "canconf"] + sys.argv[1:])


def build_type_args(spec: dict, args: argparse.Namespace) -> list[str]:
    t = ["type", "can", "bitrate", str(spec["bitrate"])]
    if "sample_point" in spec:
        t += ["sample-point", f"{spec['sample_point']}"]
    if spec.get("fd"):
        t += ["dbitrate", str(spec["dbitrate"])]
        if "dsample_point" in spec:
            t += ["dsample-point", f"{spec['dsample_point']}"]
        t += ["fd", "on"]
    if args.restart_ms is not None:
        t += ["restart-ms", str(args.restart_ms)]
    if args.listen_only:
        t += ["listen-only", "on"]
    if args.loopback:
        t += ["loopback", "on"]
    if args.one_shot:
        t += ["one-shot", "on"]
    if args.berr:
        t += ["berr-reporting", "on"]
    if args.term is not None:
        t += ["termination", str(args.term)]
    return t


def main() -> int:
    ap = argparse.ArgumentParser(
        prog="canconf", add_help=False,
        usage="canconf [OPTIONS] [SPEC]   (SPEC: e.g. 500k, 500k/2M, 500k/2M@0.8/0.75, off, up)",
    )
    ap.add_argument("spec", nargs="?")
    ap.add_argument("-i", "--ifaces")
    ap.add_argument("-r", "--restart-ms", type=int)
    ap.add_argument("--listen-only", action="store_true")
    ap.add_argument("--loopback", action="store_true")
    ap.add_argument("--one-shot", action="store_true")
    ap.add_argument("--berr", action="store_true")
    ap.add_argument("--term", type=int)
    ap.add_argument("--txqueuelen", type=int, default=DEFAULT_TXQUEUELEN)
    ap.add_argument("-n", "--dry-run", action="store_true")
    ap.add_argument("-v", "--verbose", action="store_true")
    ap.add_argument("-q", "--quiet", action="store_true")
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
        print(f"canconf {__version__}")
        return 0

    if args.ifaces:
        ifaces = [x.strip() for x in args.ifaces.split(",") if x.strip()]
    else:
        ifaces = discover_ifaces()

    if not ifaces:
        print("canconf: no CAN interfaces found (pass --ifaces to override)", file=sys.stderr)
        return 1

    if args.spec is None:
        show_status(ifaces)
        return 0

    if args.spec == "bitrates":
        show_bitrates(ifaces)
        return 0

    try:
        spec = parse_spec(args.spec)
    except ValueError as e:
        print(f"canconf: bad spec {args.spec!r}: {e}", file=sys.stderr)
        return 2

    if not args.dry_run:
        elevate_if_needed()

    for i in ifaces:
        run(["ip", "link", "set", i, "down"], dry=args.dry_run, verbose=args.verbose)

    if spec["action"] == "down":
        if not args.dry_run and not args.quiet:
            show_status(ifaces)
        return 0

    if spec["action"] == "configure":
        type_args = build_type_args(spec, args)
        for i in ifaces:
            run(["ip", "link", "set", i] + type_args, dry=args.dry_run, verbose=args.verbose)
            if args.txqueuelen is not None:
                run(["ip", "link", "set", i, "txqueuelen", str(args.txqueuelen)],
                    dry=args.dry_run, verbose=args.verbose)

    for i in ifaces:
        run(["ip", "link", "set", i, "up"], dry=args.dry_run, verbose=args.verbose)

    if not args.dry_run and not args.quiet:
        show_status(ifaces)
    return 0


if __name__ == "__main__":
    sys.exit(main())
