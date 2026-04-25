"""Helpers shared between canconf and canmon."""
from __future__ import annotations

import json
import os
import pathlib
import subprocess
import sys


# ---- colour ------------------------------------------------------------------

RESET = "\033[0m"
BOLD = "\033[1m"
DIM = "\033[2m"
RED = "\033[31m"
GREEN = "\033[32m"
YELLOW = "\033[33m"
BLUE = "\033[34m"
MAGENTA = "\033[35m"
CYAN = "\033[36m"
BRIGHT_RED = "\033[91m"
BRIGHT_YELLOW = "\033[93m"

MISSING_VALUE = "—"

_use_color: bool | None = None


def use_color() -> bool:
    global _use_color
    if _use_color is not None:
        return _use_color
    if os.environ.get("NO_COLOR"):
        _use_color = False
    elif os.environ.get("FORCE_COLOR"):
        _use_color = True
    else:
        _use_color = sys.stdout.isatty()
    return _use_color


def set_color(enabled: bool) -> None:
    global _use_color
    _use_color = enabled


def c(text: str, *codes: str) -> str:
    """Wrap text in ANSI codes if colour is enabled; otherwise return as-is."""
    if not use_color() or not codes:
        return text
    return "".join(codes) + text + RESET


STATE_STYLE = {
    "ERROR-ACTIVE":  (GREEN,),
    "ERROR-WARNING": (YELLOW,),
    "ERROR-PASSIVE": (BRIGHT_YELLOW,),
    "BUS-OFF":       (BRIGHT_RED, BOLD),
    "STOPPED":       (DIM,),
    "SLEEPING":      (DIM,),
    "UP":            (GREEN,),
    "DOWN":          (DIM,),
    "MISSING":       (RED, BOLD),
    "NO-CAN-DATA":   (YELLOW,),
}


def color_state(state: str, width: int = 0) -> str:
    """Pad state to width (if given), then wrap in its colour."""
    padded = f"{state:<{width}}" if width else state
    codes = STATE_STYLE.get(state, ())
    return c(padded, *codes) if codes else padded


# ---- interface discovery -----------------------------------------------------


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
        return {link["ifname"]: link for link in json.loads(r.stdout)}
    except (ValueError, KeyError):
        return {}
