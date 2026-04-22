"""
canconf - reconfigure all CAN / CAN-FD interfaces in one shot.

Usage:
  canconf                         show status of all can* interfaces
  canconf 500k                    classic CAN @ 500 kbit/s, up
  canconf 500k/2M                 CAN-FD: nominal 500k, data 2M, up
  canconf 500k/2M@0.8/0.75        same, with nominal/data sample points
  canconf off            | down   bring all interfaces down
  canconf up                      bring all interfaces up (no reconfigure)

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
from .common import discover_ifaces, fmt_rate, get_links

DEFAULT_TXQUEUELEN = 10000


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
        data = link.get("linkinfo", {}).get("info_data", {}) or {}
        bt = data.get("bittiming") or {}
        dbt = data.get("data_bittiming")
        mode = "CAN-FD" if dbt else "CAN"
        if bt.get("bitrate"):
            rate = fmt_rate(bt["bitrate"])
            if dbt and dbt.get("bitrate"):
                rate += f" / {fmt_rate(dbt['bitrate'])}"
        else:
            rate = "-"
        sp = bt.get("sample_point")
        dsp = (dbt or {}).get("sample_point")
        if sp and dsp:
            extra = f"sp {sp}/{dsp}"
        elif sp:
            extra = f"sp {sp}"
        else:
            extra = ""
        rows.append((name, state, mode, rate, extra))

    w_name = max(len(r[0]) for r in rows)
    w_state = max(len(r[1]) for r in rows)
    w_mode = max(len(r[2]) for r in rows)
    w_rate = max(len(r[3]) for r in rows)
    for name, state, mode, rate, extra in rows:
        print(f"{name:<{w_name}}  {state:<{w_state}}  {mode:<{w_mode}}  {rate:<{w_rate}}  {extra}".rstrip())


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
    ap.add_argument("-V", "--version", action="store_true")
    ap.add_argument("-h", "--help", action="store_true")
    args = ap.parse_args()

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
