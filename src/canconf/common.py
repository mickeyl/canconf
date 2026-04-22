"""Helpers shared between canconf and canmon."""
from __future__ import annotations

import json
import pathlib
import subprocess


def discover_ifaces() -> list[str]:
    """Return sorted names of every CAN-typed netdev on the host."""
    root = pathlib.Path("/sys/class/net")
    if not root.exists():
        return []
    found = []
    for p in sorted(root.iterdir()):
        # CAN ARPHRD is 280
        try:
            if (p / "type").read_text().strip() == "280":
                found.append(p.name)
        except OSError:
            pass
    return found


def fmt_rate(r: int) -> str:
    """500000 -> '500k', 2000000 -> '2M', other -> str(r)."""
    if r % 1_000_000 == 0:
        return f"{r // 1_000_000}M"
    if r % 1_000 == 0:
        return f"{r // 1_000}k"
    return str(r)


def get_links(stats: bool = False) -> dict[str, dict]:
    """Parse `ip -j -details [-s] link show` into a dict keyed by ifname."""
    cmd = ["ip", "-j", "-details"]
    if stats:
        cmd.append("-stats")
    cmd += ["link", "show"]
    r = subprocess.run(cmd, capture_output=True, text=True)
    try:
        return {l["ifname"]: l for l in json.loads(r.stdout)}
    except (ValueError, KeyError):
        return {}
