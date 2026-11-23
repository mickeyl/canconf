"""Bus-topology probe: which CAN interfaces share a physical bus?

Every interface in turn transmits a few tagged classic CAN frames; all the
other interfaces listen on raw sockets. An interface that sees the tag is on
the same bus as the sender. SocketCAN never loops frames between netdevs, so
a frame can only cross from one iface to another through real wiring (or a
gateway/bridge that forwards verbatim).

Interfaces that hear nothing are reported as lone nodes. That is ambiguous by
nature: either the iface really is alone, or no other node ACKs. A frame
without ACK stays in the controller's TX mailbox and is retried until some
node shows up; the payload carries a per-run nonce so such stale frames are
ignored by later runs.
"""
from __future__ import annotations

import os
import random
import select
import socket
import struct
import sys
import time

from . import common
from .common import c, fmt_rate, get_links

# Inlined so the module imports on Python builds without socket.CAN_RAW etc.
AF_CAN = getattr(socket, "AF_CAN", 29)
CAN_RAW = getattr(socket, "CAN_RAW", 1)
CAN_EFF_FLAG = 0x80000000
FRAME = struct.Struct("=IB3x8s")

# Extended ID near the bottom of the arbitration priority range, so probes
# mostly lose against real traffic on a live bus.
PROBE_ID_BASE = 0x1FFF0000   # | per-run nonce (16 bit)
MAGIC = b"CT"
# magic(2) nonce(2) sender-index(1) seq(1) pad(2)
PAYLOAD = struct.Struct("=2sHBB2x")

DEFAULT_COUNT = 2
DEFAULT_WINDOW = 0.1   # seconds to listen after each probe frame


class ProbeResult:
    def __init__(self, ifaces: list[str]):
        self.ifaces = ifaces
        # sender -> set of interfaces that received its probe
        self.heard: dict[str, set[str]] = {i: set() for i in ifaces}
        # iface -> reason it could not take part
        self.errors: dict[str, str] = {}


def _open(name: str) -> socket.socket:
    s = socket.socket(AF_CAN, socket.SOCK_RAW, CAN_RAW)
    try:
        s.bind((name,))
        s.setblocking(False)
    except OSError:
        s.close()
        raise
    return s


def probe(ifaces: list[str], count: int = DEFAULT_COUNT,
          window: float = DEFAULT_WINDOW) -> ProbeResult:
    """Transmit probes on each iface and record who hears them.

    A frame is attributed to its sender by the index in its payload, not by
    whose turn it is, so a late-delivered frame still counts. The nonce also
    goes into the arbitration ID, so two concurrent runs on one bus never
    transmit identical IDs with different data.
    """
    res = ProbeResult(ifaces)
    socks: dict[str, socket.socket] = {}
    for name in ifaces:
        try:
            socks[name] = _open(name)
        except OSError as e:
            res.errors[name] = e.strerror or str(e)
    try:
        nonce = random.getrandbits(16)
        by_sock = {s: n for n, s in socks.items()}
        idx_name = dict(enumerate(ifaces))

        def listen(until: float) -> None:
            listeners = list(socks.values())
            while listeners:
                left = until - time.monotonic()
                if left <= 0:
                    return
                ready, _, _ = select.select(listeners, [], [], left)
                for s in ready:
                    _collect(s, by_sock[s], nonce, idx_name, res, until)

        for idx, src in enumerate(ifaces):
            if src not in socks:
                continue
            for seq in range(count):
                data = PAYLOAD.pack(MAGIC, nonce, idx & 0xFF, seq)
                frame = FRAME.pack(PROBE_ID_BASE | nonce | CAN_EFF_FLAG,
                                   len(data), data.ljust(8, b"\0"))
                try:
                    socks[src].send(frame)
                except OSError as e:
                    res.errors[src] = e.strerror or str(e)
                    break
                listen(time.monotonic() + window)
        # Grace period for frames still queued in a slow adapter.
        listen(time.monotonic() + window * 2)
    finally:
        for s in socks.values():
            s.close()
    return res


def _collect(s, dst, nonce, idx_name, res, until) -> None:
    """Read what is pending on s, but never past the deadline."""
    while time.monotonic() < until:
        try:
            raw = s.recv(FRAME.size)
        except (BlockingIOError, OSError):
            return
        if len(raw) < FRAME.size:
            continue
        _, dlc, data = FRAME.unpack(raw)
        if dlc < PAYLOAD.size:
            continue
        magic, n, i, _ = PAYLOAD.unpack(data[:PAYLOAD.size])
        src = idx_name.get(i)
        if magic == MAGIC and n == nonce and src and src != dst:
            res.heard[src].add(dst)


# ---- analysis ----------------------------------------------------------------


def group_buses(res: ProbeResult) -> list[list[str]]:
    """Connected components over 'A heard B' or 'B heard A' (undirected)."""
    members = [i for i in res.ifaces if i not in res.errors]
    parent = {i: i for i in members}

    def find(x: str) -> str:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for src, dsts in res.heard.items():
        for d in dsts:
            if src in parent and d in parent:
                parent[find(src)] = find(d)
    groups: dict[str, list[str]] = {}
    for i in members:
        groups.setdefault(find(i), []).append(i)
    # Multi-node buses first, then by first member name.
    return sorted(groups.values(), key=lambda g: (len(g) == 1, g[0]))


def asymmetric_pairs(res: ProbeResult) -> list[tuple[str, str]]:
    """(a, b) where b heard a but a did not hear b."""
    out = []
    for a, dsts in res.heard.items():
        for b in sorted(dsts):
            if a not in res.heard.get(b, set()) and b not in res.errors:
                out.append((a, b))
    return out


def usable(link: dict | None) -> str | None:
    """Return None if the iface can take part, else the reason it cannot."""
    if link is None:
        return "missing"
    if "UP" not in (link.get("flags") or []):
        return "down"
    data = (link.get("linkinfo") or {}).get("info_data") or {}
    if data.get("state") in ("BUS-OFF", "STOPPED"):
        return data["state"].lower()
    mode = {str(m).upper() for m in (data.get("ctrlmode") or [])}
    if "LISTEN-ONLY" in mode:
        return "listen-only: cannot transmit"
    if "LOOPBACK" in mode:
        return "loopback mode: frames never reach the wire"
    return None


def _counters(link: dict | None) -> dict:
    link = link or {}
    info = link.get("linkinfo") or {}
    st = link.get("stats64") or {}
    xs = info.get("info_xstats") or {}
    return {
        "state": (info.get("info_data") or {}).get("state") or "",
        "rx": (st.get("rx") or {}).get("packets", 0),
        "tx": (st.get("tx") or {}).get("packets", 0),
        "txerr": (st.get("tx") or {}).get("errors", 0),
        "bus_error": xs.get("bus_error", 0),
        "warn": xs.get("error_warning", 0),
        "passive": xs.get("error_passive", 0),
        "bus_off": xs.get("bus_off", 0),
    }


def diagnose(before: dict | None, after: dict | None) -> str:
    """One line of kernel-counter evidence for an iface that heard nobody."""
    b, a = _counters(before), _counters(after)
    d = {k: a[k] - b[k] for k in a if k != "state"}
    parts = [a["state"] or "no state info",
             f"rx {d['rx']:+d} tx {d['tx']:+d}"]
    errs = d["bus_error"] + d["txerr"] + d["warn"] + d["passive"] + d["bus_off"]
    if errs:
        parts.append(f"errors during probe: bus {d['bus_error']:+d}, "
                     f"warn {d['warn']:+d}, passive {d['passive']:+d}, "
                     f"bus-off {d['bus_off']:+d}")
    if a["state"] in ("ERROR-PASSIVE", "ERROR-WARNING", "BUS-OFF"):
        parts.append("controller is in an error state — frames are probably "
                     "not being ACKed (no peer, wiring, termination or bitrate)")
    elif d["rx"] == d["tx"] and errs == 0:
        parts.append("no errors — likely alone on a quiet bus, or the bus "
                     "ACKs but no other probed interface is connected")
    return ", ".join(parts)


def rate_label(link: dict | None) -> str:
    data = ((link or {}).get("linkinfo") or {}).get("info_data") or {}
    bt = data.get("bittiming") or {}
    dbt = data.get("data_bittiming") or {}
    if not bt.get("bitrate"):
        return ""
    s = fmt_rate(bt["bitrate"])
    if dbt.get("bitrate"):
        s += "/" + fmt_rate(dbt["bitrate"])
    return s


def driver_label(name: str, link: dict | None,
                 sysfs: str = "/sys/class/net") -> str:
    """Best-effort driver name: bittiming_const, then sysfs, then link kind."""
    info = (link or {}).get("linkinfo") or {}
    label = ((info.get("info_data") or {}).get("bittiming_const") or {}).get("name")
    if label:
        return label
    try:
        return os.path.basename(os.readlink(f"{sysfs}/{name}/device/driver"))
    except OSError:
        pass
    return info.get("info_kind") or ""


# ---- rendering ---------------------------------------------------------------

_ASCII = str.maketrans("═╤│┌┐└┘─┴", "=+|++++-+")


def _unicode_ok() -> bool:
    enc = (getattr(sys.stdout, "encoding", None) or "").lower()
    return "utf" in enc


def render_bus(title: str, nodes: list[tuple[str, str]]) -> list[str]:
    """Draw one bus with its nodes hanging off it. nodes = [(name, label)]."""
    gap = 3
    widths = [max(len(n), len(lbl)) + 2 for n, lbl in nodes]
    starts, x = [], 2
    for w in widths:
        starts.append(x)
        x += w + 2 + gap
    centers = [s + (w + 1) // 2 for s, w in zip(starts, widths)]
    total = x - gap + 2

    bus = list("═" * total)
    stems = [" "] * total
    top = [" "] * total
    mid = [" "] * total
    bot = [" "] * total
    lbl = [" "] * total

    def put(row, col, text):
        for k, ch in enumerate(text):
            row[col + k] = ch

    for (name, label), s, w, cx in zip(nodes, starts, widths, centers):
        bus[cx] = "╤"
        stems[cx] = "│"
        put(top, s, "┌" + "─" * w + "┐")
        top[cx] = "┴"
        put(mid, s, "│" + f" {name} ".center(w) + "│")
        put(bot, s, "└" + "─" * w + "┘")
        if label:
            put(lbl, s, label.center(w + 2).rstrip())
    rows = [title, "".join(bus), "".join(stems).rstrip(),
            "".join(top).rstrip(), "".join(mid).rstrip(),
            "".join(bot).rstrip(), "".join(lbl).rstrip()]
    return [r for r in rows if r]


def render(res: ProbeResult, links: dict[str, dict],
           skipped: dict[str, str],
           diag: dict[str, str] | None = None) -> list[str]:
    buses = group_buses(res)
    out: list[str] = []
    joined = [b for b in buses if len(b) > 1]
    lone = [b[0] for b in buses if len(b) == 1]
    n_probed = len(res.ifaces) - len(res.errors)

    for k, members in enumerate(buses, 1):
        nodes = [(m, driver_label(m, links.get(m))) for m in members]
        rates = {r for r in (rate_label(links.get(m)) for m in members) if r}
        rate = next(iter(rates)) if len(rates) == 1 else ""
        if len(members) > 1:
            title = (c(f"Bus {k}", common.BOLD)
                     + c(f"  {len(members)} nodes" + (f"  {rate}" if rate else ""),
                         common.DIM))
            if len(rates) > 1:
                title += "  " + c("bitrates differ!", common.BRIGHT_RED, common.BOLD)
        else:
            title = (c(f"Bus {k}", common.BOLD)
                     + c("  no other interface heard on this bus"
                         + (f"  {rate}" if rate else ""), common.DIM))
        out += render_bus(title, nodes)
        out.append("")

    for name in lone:
        if diag and name in diag:
            out.append(c(f"  {name}: {diag[name]}", common.DIM))
    for name, why in sorted(skipped.items()):
        out.append(c(f"  {name}: not probed ({why})", common.DIM))
    for name, why in sorted(res.errors.items()):
        out.append(c(f"  {name}: probe failed ({why})", common.RED))
    for a, b in asymmetric_pairs(res):
        out.append(c(f"  warning: {b} hears {a}, but {a} does not hear {b} "
                     f"(listen-only or one-way link?)", common.YELLOW))

    summary = (f"{n_probed} interface{'s' if n_probed != 1 else ''} probed, "
               f"{len(buses)} bus{'es' if len(buses) != 1 else ''}")
    if lone:
        summary += (f"; {len(lone)} without a peer ({', '.join(lone)}) — "
                    f"alone on its bus, or no other node ACKs")
    out.append(c(summary, common.BOLD))
    if not joined and n_probed > 1:
        out.append(c("hint: no two interfaces share a bus (virtual CAN, "
                     "unconnected, loopback, or different bitrates)", common.DIM))
    text = "\n".join(out)
    if not _unicode_ok():
        text = text.translate(_ASCII)
    return text.split("\n")


def show_topology(ifaces: list[str], *, settle: float = 0.0) -> int:
    """Probe and print the bus topology. Returns a process exit code."""
    if settle:
        time.sleep(settle)
    links = get_links()
    skipped: dict[str, str] = {}
    candidates: list[str] = []
    for name in ifaces:
        why = usable(links.get(name))
        if why:
            skipped[name] = why
        else:
            candidates.append(name)
    if not candidates:
        print("canconf: no CAN interface is up — nothing to probe", file=sys.stderr)
        for name, why in skipped.items():
            print(f"  {name}: {why}", file=sys.stderr)
        return 1
    before = get_links(stats=True)
    res = probe(candidates)
    after = get_links(stats=True)
    diag = {n: diagnose(before.get(n), after.get(n)) for n in candidates}
    print("\n".join(render(res, after, skipped, diag)))
    return 1 if res.errors else 0
