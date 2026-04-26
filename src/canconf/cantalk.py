"""
cantalk - tiny REPL for sending and receiving CAN messages.

Usage:
  cantalk IFACE                       ISOTP mode (default)
  cantalk IFACE 7DF,7E8               ISOTP, pre-set tx=7DF rx=7E8
  cantalk IFACE 7DF                   ISOTP, tx=7DF, rx auto-derived
  cantalk IFACE --raw                 raw CAN mode (no segmentation)
  cantalk IFACE 7DF --raw             raw, send id=7DF, rx accept-all

REPL:
  :7DF                  set tx=0x7DF, derive rx (tx+8 / J1939 swap)
  :7DF,7E8              set tx=0x7DF, rx=0x7E8
  :18DA10F1,18DAF110    29-bit extended addressing
  :info                 show the current state
  :help                 show this help
  0902 / 09 02          send hex bytes (one ISOTP message or one raw frame)
  quit / Ctrl-C / Ctrl-D  exit

Options:
  --raw                 use raw CAN sockets instead of ISOTP
  -t, --timeout SEC     response timeout (default: 2.0)
  -p, --pad HEX         pad every CAN frame to 8 bytes with this byte
                        (default: AA; pass 'none' to disable)
  --plain               don't use the bottom-anchored TUI (force the simple line-by-line REPL)
  --no-color            disable ANSI colour
  -V, --version         print version and exit
  -h, --help            show this help

Default mode is ISOTP (ISO 15765-2): the kernel's can-isotp module handles
segmentation, flow control, and reassembly transparently. Pass --raw to
bypass it and exchange single 8-byte CAN frames directly. In both modes
short frames are padded to 8 bytes with the --pad byte (most ECUs expect
this).

Requires the interface to be already up (see canconf) and CAP_NET_RAW or root.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import select
import signal
import socket
import struct
import sys
import time

from . import __version__
from . import common
from .common import (
    BLUE, BOLD, BRIGHT_RED, CYAN, DIM, GREEN, MAGENTA, RESET, YELLOW,
    c,
)

# ── SocketCAN constants ──────────────────────────────────────────────────────
# Pulled from <linux/can.h> / <linux/can/isotp.h>; kept as plain ints so the
# module imports cleanly even on systems where socket.CAN_ISOTP isn't exposed.
CAN_RAW = getattr(socket, "CAN_RAW", 1)
CAN_ISOTP = getattr(socket, "CAN_ISOTP", 6)
CAN_EFF_FLAG = 0x80000000
CAN_SFF_MASK = 0x000007FF
CAN_EFF_MASK = 0x1FFFFFFF
SOL_CAN_BASE = 100
SOL_CAN_RAW = SOL_CAN_BASE + CAN_RAW
SOL_CAN_ISOTP = SOL_CAN_BASE + CAN_ISOTP
CAN_RAW_FILTER = 1
CAN_ISOTP_OPTS = 1

# struct can_isotp_options (linux/can/isotp.h):
#   __u32 flags;          __u32 frame_txtime;
#   __u8  ext_address;    __u8  txpad_content;
#   __u8  rxpad_content;  __u8  rx_ext_address;
ISOTP_OPTS_FMT = "=IIBBBB"
ISOTP_TX_PADDING = 0x0004
ISOTP_RX_PADDING = 0x0008

CAN_FRAME_FMT = "=IB3x8s"
CAN_FRAME_SIZE = struct.calcsize(CAN_FRAME_FMT)

DEFAULT_TIMEOUT = 2.0
DEFAULT_PADDING = 0xAA  # most ECUs expect frames padded to 8 bytes
RAW_QUIET_PERIOD = 0.25  # extend deadline by this after each received frame
HISTORY_MAX = 1000


# ── ANSI / TUI helpers ───────────────────────────────────────────────────────
_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]")


def _vlen(s: str) -> int:
    """Visible (printable) length, ignoring ANSI escape sequences."""
    return len(_ANSI_RE.sub("", s))


def _move(row: int, col: int = 1) -> str:
    return f"\033[{row};{col}H"


def _scroll_region(top: int, bottom: int) -> str:
    return f"\033[{top};{bottom}r"


_RESET_SCROLL = "\033[r"
_CLEAR_LINE = "\033[2K"
_CLEAR_BELOW = "\033[J"
_HIDE = "\033[?25l"
_SHOW = "\033[?25h"
_SAVE = "\0337"   # DECSC — better preserved across DECSTBM than \033[s
_RESTORE = "\0338"


# ── Hex and id parsing ───────────────────────────────────────────────────────
_HEX_DIGITS = set("0123456789abcdefABCDEF")
_HEX_SEPS = set(" \t:,")


def parse_hex(text: str) -> bytes:
    """Parse hex bytes. ' ', ':', ',', and tabs are accepted as separators."""
    cleaned = "".join(ch for ch in text if ch not in _HEX_SEPS)
    if not cleaned:
        raise ValueError("empty payload")
    bad = next((ch for ch in cleaned if ch not in _HEX_DIGITS), None)
    if bad is not None:
        raise ValueError(f"non-hex character {bad!r}")
    if len(cleaned) % 2:
        cleaned = "0" + cleaned
    return bytes.fromhex(cleaned)


def parse_id(text: str) -> int:
    text = text.strip()
    if not text:
        raise ValueError("empty CAN id")
    if not all(ch in _HEX_DIGITS for ch in text):
        raise ValueError(f"bad CAN id {text!r}")
    val = int(text, 16)
    if val > CAN_EFF_MASK:
        raise ValueError(f"CAN id 0x{val:X} exceeds 29-bit max")
    return val


def fmt_id(can_id: int) -> str:
    return f"{can_id:08X}" if can_id > CAN_SFF_MASK else f"{can_id:03X}"


def derive_rx(tx: int) -> int:
    """OBD2 convention for 11-bit (rx = tx + 8); J1939-ish swap for 29-bit
    physical-addressing IDs (18DA<target><source>); else fall back to tx + 8."""
    if tx <= CAN_SFF_MASK:
        return tx + 8
    if (tx & 0xFFFF0000) == 0x18DA0000:
        target = (tx >> 8) & 0xFF
        source = tx & 0xFF
        return 0x18DA0000 | (source << 8) | target
    return tx + 8


# ── Socket plumbing ──────────────────────────────────────────────────────────
def _eff_kid(can_id: int) -> int:
    return can_id | (CAN_EFF_FLAG if can_id > CAN_SFF_MASK else 0)


def open_isotp(iface: str, tx_id: int, rx_id: int, padding: int | None) -> socket.socket:
    s = socket.socket(socket.AF_CAN, socket.SOCK_DGRAM, CAN_ISOTP)
    if padding is not None:
        flags = ISOTP_TX_PADDING | ISOTP_RX_PADDING
        opts = struct.pack(
            ISOTP_OPTS_FMT, flags, 0, 0, padding & 0xFF, padding & 0xFF, 0
        )
        s.setsockopt(SOL_CAN_ISOTP, CAN_ISOTP_OPTS, opts)
    # Python's ISOTP bind tuple is (interface, rx_id, tx_id).
    s.bind((iface, _eff_kid(rx_id), _eff_kid(tx_id)))
    return s


def open_raw(iface: str, rx_id: int | None) -> socket.socket:
    s = socket.socket(socket.AF_CAN, socket.SOCK_RAW, CAN_RAW)
    if rx_id is not None:
        eff = rx_id > CAN_SFF_MASK
        kid = _eff_kid(rx_id)
        kmask = (CAN_EFF_MASK | CAN_EFF_FLAG) if eff else CAN_SFF_MASK
        s.setsockopt(SOL_CAN_RAW, CAN_RAW_FILTER, struct.pack("=II", kid, kmask))
    s.bind((iface,))
    return s


def send_raw_frame(
    sock: socket.socket, can_id: int, data: bytes, padding: int | None = None
) -> None:
    if len(data) > 8:
        raise ValueError(f"raw CAN frame is max 8 bytes ({len(data)} given)")
    dlc = len(data)
    if padding is not None and dlc < 8:
        data = data + bytes([padding & 0xFF]) * (8 - dlc)
        dlc = 8
    frame = struct.pack(CAN_FRAME_FMT, _eff_kid(can_id), dlc, data.ljust(8, b"\x00"))
    sock.send(frame)


def recv_raw_frame(sock: socket.socket) -> tuple[int, bytes]:
    raw = sock.recv(CAN_FRAME_SIZE)
    can_id, dlc, payload = struct.unpack(CAN_FRAME_FMT, raw)
    can_id &= CAN_EFF_MASK if (can_id & CAN_EFF_FLAG) else CAN_SFF_MASK
    return can_id, payload[:dlc]


# ── REPL state ───────────────────────────────────────────────────────────────
class State:
    def __init__(self, iface: str, raw: bool, timeout: float, padding: int | None):
        self.iface = iface
        self.raw = raw
        self.timeout = timeout
        self.padding = padding
        self.tx_id: int | None = None
        self.rx_id: int | None = None
        self.sock: socket.socket | None = None

    def close_socket(self) -> None:
        if self.sock is not None:
            try:
                self.sock.close()
            finally:
                self.sock = None

    def set_arbitration(self, tx: int, rx: int | None) -> None:
        if not self.raw and rx is None:
            raise ValueError("ISOTP requires both tx and rx ids")
        self.close_socket()
        if self.raw:
            self.sock = open_raw(self.iface, rx)
        else:
            self.sock = open_isotp(self.iface, tx, rx, self.padding)
        self.tx_id, self.rx_id = tx, rx

    # ── Visual descriptions ──
    def mode_label(self) -> str:
        return "raw" if self.raw else "isotp"

    def pad_label(self) -> str:
        return f"0x{self.padding:02X}" if self.padding is not None else "off"

    def arb_label(self, plain: bool = False) -> str:
        """Coloured 'TX → RX' (or 'unset')."""
        if self.tx_id is None:
            return "unset" if plain else c("unset", DIM)
        rx = fmt_id(self.rx_id) if self.rx_id is not None else "*"
        if plain:
            return f"{fmt_id(self.tx_id)} → {rx}"
        return f"{c(fmt_id(self.tx_id), CYAN)} {c('→', DIM)} {c(rx, MAGENTA)}"

    def info_segments(self) -> list[str]:
        """Coloured segments for the bottom info bar."""
        mode_color = YELLOW if self.raw else GREEN
        return [
            c(self.iface, BOLD),
            c(self.mode_label(), mode_color),
            self.arb_label(),
            f"pad={self.pad_label()}",
            f"timeout={self.timeout:g}s",
        ]

    def simple_prompt(self) -> str:
        """Prompt for the plain-fallback REPL (no scroll region)."""
        if self.tx_id is None:
            return c(self.iface, BOLD) + c(f" [{self.mode_label()}] ❯ ", DIM)
        return (
            c(self.iface, BOLD) + " "
            + c(fmt_id(self.tx_id), CYAN) + c("→", DIM)
            + (c(fmt_id(self.rx_id), MAGENTA) if self.rx_id is not None else c("*", DIM))
            + f" [{c(self.mode_label(), GREEN if not self.raw else YELLOW)}] "
            + c("❯ ", BOLD)
        )


# ── Display helpers (route through the active term, if any) ──────────────────
_active_term: "Term | None" = None


def log(text: str = "") -> None:
    if _active_term is not None:
        _active_term.log(text)
    else:
        print(text)


def fmt_bytes(b: bytes) -> str:
    return " ".join(f"{x:02X}" for x in b) or "(empty)"


def fmt_ascii(b: bytes) -> str:
    return "".join(chr(x) if 32 <= x < 127 else "." for x in b)


def print_request(state: State, payload: bytes) -> None:
    arrow = c("→", CYAN)
    head = c(fmt_id(state.tx_id), CYAN) if state.tx_id is not None else "?"
    log(f"  {arrow} {head:>8}  {c(fmt_bytes(payload), DIM)}")


def print_response(can_id: int, payload: bytes) -> None:
    arrow = c("←", BLUE)
    header = c(fmt_id(can_id), BLUE)
    log(f"  {arrow} {header:>8}  {c(fmt_bytes(payload), YELLOW)}")
    if payload:
        log(f"          {c('->', BRIGHT_RED)}  {c(fmt_ascii(payload), GREEN)}")
    note = interpret(payload)
    if note:
        log(f"          {c('ℹ︎', CYAN)}  {note}")


# ── Tiny UDS / OBD2 interpreter ──────────────────────────────────────────────
NRC_NAMES = {
    0x10: "general reject",
    0x11: "service not supported",
    0x12: "sub-function not supported",
    0x13: "incorrect message length / invalid format",
    0x14: "response too long",
    0x21: "busy - repeat request",
    0x22: "conditions not correct",
    0x24: "request sequence error",
    0x25: "no response from sub-net component",
    0x26: "failure prevents execution of requested action",
    0x31: "request out of range",
    0x33: "security access denied",
    0x35: "invalid key",
    0x36: "exceeded number of attempts",
    0x37: "required time delay not expired",
    0x70: "upload/download not accepted",
    0x71: "transfer data suspended",
    0x72: "general programming failure",
    0x73: "wrong block sequence counter",
    0x78: "request received - response pending",
    0x7E: "sub-function not supported in active session",
    0x7F: "service not supported in active session",
}

UDS_SERVICES = {
    0x10: "Diagnostic Session Control",
    0x11: "ECU Reset",
    0x14: "Clear Diagnostic Information",
    0x19: "Read DTC Information",
    0x22: "Read Data By Identifier",
    0x23: "Read Memory By Address",
    0x27: "Security Access",
    0x28: "Communication Control",
    0x2E: "Write Data By Identifier",
    0x2F: "Input/Output Control By Identifier",
    0x31: "Routine Control",
    0x34: "Request Download",
    0x35: "Request Upload",
    0x36: "Transfer Data",
    0x37: "Request Transfer Exit",
    0x3E: "Tester Present",
    0x85: "Control DTC Setting",
}

OBD2_MODES = {
    0x01: "Show Current Data",
    0x02: "Show Freeze Frame",
    0x03: "Show Stored DTCs",
    0x04: "Clear DTCs",
    0x05: "O2 Sensor Monitoring",
    0x06: "On-Board Monitoring",
    0x07: "Show Pending DTCs",
    0x08: "Control On-Board System",
    0x09: "Vehicle Information",
    0x0A: "Permanent DTCs",
}


def interpret(b: bytes) -> str | None:
    if not b:
        return None
    sid = b[0]
    if sid == 0x7F and len(b) >= 3:
        svc = UDS_SERVICES.get(b[1], f"service 0x{b[1]:02X}")
        nrc = NRC_NAMES.get(b[2], f"NRC 0x{b[2]:02X}")
        return c(f"✗ negative response to {svc}: {nrc}", BRIGHT_RED)
    if 0x41 <= sid <= 0x4A:
        mode = OBD2_MODES.get(sid - 0x40, f"Mode 0x{sid - 0x40:02X}")
        return c(f"✓ OBD2 {mode}", GREEN)
    if sid >= 0x50:
        svc = UDS_SERVICES.get(sid - 0x40, f"service 0x{sid - 0x40:02X}")
        return c(f"✓ positive response to {svc}", GREEN)
    return None


# ── Send / receive ───────────────────────────────────────────────────────────
def send_and_receive(state: State, payload: bytes) -> None:
    if state.sock is None or state.tx_id is None:
        return
    print_request(state, payload)
    try:
        if state.raw:
            send_raw_frame(state.sock, state.tx_id, payload, padding=state.padding)
            collect_raw(state)
        else:
            state.sock.send(payload)
            collect_isotp(state)
    except OSError as e:
        log(f"  {c('!', BRIGHT_RED)} send/recv error: {e}")


def collect_isotp(state: State) -> None:
    state.sock.settimeout(state.timeout)
    try:
        data = state.sock.recv(4096)
    except socket.timeout:
        log(f"  {c('·', DIM)} no response within {state.timeout:g}s")
        return
    finally:
        state.sock.settimeout(None)
    print_response(state.rx_id if state.rx_id is not None else 0, data)


def collect_raw(state: State) -> None:
    deadline = time.monotonic() + state.timeout
    got_any = False
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        rd, _, _ = select.select([state.sock], [], [], remaining)
        if not rd:
            break
        try:
            can_id, payload = recv_raw_frame(state.sock)
        except OSError:
            break
        print_response(can_id, payload)
        got_any = True
        deadline = time.monotonic() + RAW_QUIET_PERIOD
    if not got_any:
        log(f"  {c('·', DIM)} no response within {state.timeout:g}s")


# ── Commands ─────────────────────────────────────────────────────────────────
def handle_command(state: State, body: str) -> None:
    body = body.strip()
    if body in ("", "info", "i"):
        show_info(state)
        return
    if body in ("help", "h", "?"):
        show_help()
        return
    parts = [p.strip() for p in body.split(",")]
    if len(parts) > 2:
        log(f"  {c('!', BRIGHT_RED)} usage: :TX  or  :TX,RX")
        return
    try:
        tx = parse_id(parts[0])
    except ValueError as e:
        log(f"  {c('!', BRIGHT_RED)} {e}")
        return
    rx: int | None
    if len(parts) == 2 and parts[1]:
        try:
            rx = parse_id(parts[1])
        except ValueError as e:
            log(f"  {c('!', BRIGHT_RED)} {e}")
            return
    else:
        rx = None if state.raw else derive_rx(tx)
        if rx is not None:
            log(f"  {c('·', DIM)} rx auto-derived: {fmt_id(rx)}")
    try:
        state.set_arbitration(tx, rx)
    except (OSError, ValueError) as e:
        log(f"  {c('!', BRIGHT_RED)} {e}")
        return
    show_info(state)
    if _active_term is not None:
        _active_term.draw_prompt()


def show_help() -> None:
    log()
    rows = [
        (":7DF",                "set tx=0x7DF; rx auto-derived"),
        (":7DF,7E8",            "set tx=0x7DF, rx=0x7E8"),
        (":18DA10F1,18DAF110",  "29-bit extended addressing"),
        (":info",               "show current state"),
        (":help",               "show this help"),
        ("0902 / 09 02",        "send hex bytes (one ISOTP message or one frame)"),
        ("quit / Ctrl-C / Ctrl-D", "exit"),
    ]
    width = max(len(k) for k, _ in rows)
    for k, v in rows:
        log(f"  {c(k.ljust(width), CYAN)}  {c(v, DIM)}")
    log()


def show_info(state: State) -> None:
    pad = state.pad_label()
    if state.tx_id is None:
        log(
            f"  {c('·', DIM)} iface={c(state.iface, BOLD)}  mode={state.mode_label()}  "
            f"arbitration={c('unset', DIM)}  timeout={state.timeout:g}s  pad={pad}"
        )
        return
    rx = fmt_id(state.rx_id) if state.rx_id is not None else "*"
    log(
        f"  {c('·', DIM)} iface={c(state.iface, BOLD)}  mode={state.mode_label()}  "
        f"tx={c(fmt_id(state.tx_id), CYAN)}  rx={c(rx, MAGENTA)}  "
        f"timeout={state.timeout:g}s  pad={pad}"
    )


# ── Term: TUI with bottom-anchored prompt + scroll region above ──────────────
PROMPT_HEIGHT = 3  # info bar + input line + bottom rule


class Term:
    """Bottom-anchored prompt. Logs scroll independently in the area above.

    Falls back to plain line-by-line input() when stdin/stdout isn't a TTY,
    or when constructed with active=False.
    """

    def __init__(self, state: State, history: list[str], *, active: bool = True):
        self.state = state
        self.history = history
        self.is_tty = (
            active and sys.stdin.isatty() and sys.stdout.isatty()
            and os.environ.get("TERM") not in (None, "", "dumb")
        )
        self.fd = sys.stdin.fileno() if self.is_tty else -1
        self._saved_termios = None
        self._prev_winch = None
        self.rows = 24
        self.cols = 80
        # Line editor state (only used in TUI mode):
        self.buf = ""
        self.cur = 0
        self.hist_idx = 0
        self.scratch = ""

    # ---- lifecycle ----
    def __enter__(self) -> "Term":
        if not self.is_tty:
            return self
        import termios, tty  # local — only needed in TUI mode
        self._saved_termios = termios.tcgetattr(self.fd)
        tty.setcbreak(self.fd)
        self._refresh_size()
        sys.stdout.write(_HIDE)
        # Scroll region for the area above the prompt; clear what's there so
        # we don't pollute scrollback with our prompt setup.
        sys.stdout.write(_scroll_region(1, max(1, self.rows - PROMPT_HEIGHT)))
        sys.stdout.write(_move(self.rows - PROMPT_HEIGHT, 1))
        sys.stdout.write(_CLEAR_LINE)
        # Park cursor at the bottom of the scroll region so future log()s
        # accumulate downward and only start scrolling once full.
        sys.stdout.write(_SHOW)
        sys.stdout.flush()
        self._prev_winch = signal.getsignal(signal.SIGWINCH)
        signal.signal(signal.SIGWINCH, self._on_winch)
        self.draw_prompt()
        return self

    def __exit__(self, *_: object) -> None:
        if not self.is_tty:
            return
        import termios
        try:
            signal.signal(signal.SIGWINCH, self._prev_winch or signal.SIG_DFL)
        except (TypeError, ValueError):
            pass
        # Reset scroll region, clear our prompt area, leave the cursor on a
        # fresh line so the user's shell prompt doesn't overwrite anything.
        sys.stdout.write(_RESET_SCROLL)
        sys.stdout.write(_move(max(1, self.rows - PROMPT_HEIGHT + 1), 1))
        sys.stdout.write(_CLEAR_BELOW)
        sys.stdout.write(_SHOW)
        sys.stdout.flush()
        try:
            termios.tcsetattr(self.fd, termios.TCSADRAIN, self._saved_termios)
        except OSError:
            pass

    # ---- terminal size ----
    def _refresh_size(self) -> None:
        try:
            sz = os.get_terminal_size()
            self.rows = max(PROMPT_HEIGHT + 1, sz.lines)
            self.cols = max(20, sz.columns)
        except OSError:
            self.rows, self.cols = 24, 80

    def _on_winch(self, *_: object) -> None:
        self._refresh_size()
        sys.stdout.write(_scroll_region(1, max(1, self.rows - PROMPT_HEIGHT)))
        self.draw_prompt()

    # ---- output ----
    def log(self, text: str) -> None:
        """Print a (possibly multi-line) line into the scroll area above the prompt."""
        if not self.is_tty:
            print(text)
            return
        sys.stdout.write(_HIDE)
        sys.stdout.write(_SAVE)
        target = max(1, self.rows - PROMPT_HEIGHT)
        for line in (text or "").split("\n"):
            sys.stdout.write(_move(target, 1))
            # \n at the bottom of the scroll region scrolls the region up
            # without disturbing the prompt area below it.
            sys.stdout.write(line + "\n")
        sys.stdout.write(_RESTORE)
        sys.stdout.write(_SHOW)
        sys.stdout.flush()

    def draw_prompt(self) -> None:
        if not self.is_tty:
            return
        sys.stdout.write(_HIDE)
        info_row = max(1, self.rows - 2)
        input_row = max(1, self.rows - 1)
        rule_row = self.rows
        # Info bar (top of prompt area).
        sys.stdout.write(_move(info_row, 1))
        sys.stdout.write(_CLEAR_LINE)
        sys.stdout.write(self._build_rule(self.state.info_segments()))
        # Input line (cursor lives here).
        sys.stdout.write(_move(input_row, 1))
        sys.stdout.write(_CLEAR_LINE)
        sys.stdout.write(c("❯ ", BOLD) + self.buf)
        # Bottom rule with hints.
        sys.stdout.write(_move(rule_row, 1))
        sys.stdout.write(_CLEAR_LINE)
        sys.stdout.write(self._build_rule(self._hint_segments()))
        # Cursor on input line. Column = 1 + 2 (for "❯ ") + cur (ASCII-only assumed).
        sys.stdout.write(_move(input_row, 1 + 2 + self.cur))
        sys.stdout.write(_SHOW)
        sys.stdout.flush()

    def _build_rule(self, segments: list[str]) -> str:
        sep = c(" · ", DIM)
        body = "  " + sep.join(segments) + "  "
        n_visible = _vlen(body)
        right = max(0, self.cols - n_visible - 2)
        return c("──", DIM) + body + c("─" * right, DIM)

    def _hint_segments(self) -> list[str]:
        return [
            c(":help", CYAN),
            c("↑↓", CYAN) + c(" history", DIM),
            c("Ctrl-C", CYAN) + c(" exit", DIM),
        ]

    # ---- input ----
    def read_line(self) -> str:
        """Read one line. Raises EOFError on Ctrl-D (empty buffer),
        KeyboardInterrupt on Ctrl-C."""
        if not self.is_tty:
            line = input(self.state.simple_prompt())
            if line and (not self.history or self.history[-1] != line):
                self.history.append(line)
                if len(self.history) > HISTORY_MAX:
                    del self.history[: -HISTORY_MAX]
            return line

        self.buf = ""
        self.cur = 0
        self.hist_idx = len(self.history)
        self.scratch = ""
        self.draw_prompt()

        while True:
            key = self._read_key()
            if key is None:
                continue
            if key == "INT":
                raise KeyboardInterrupt
            if key == "EOF":
                if not self.buf:
                    raise EOFError
                if self.cur < len(self.buf):
                    self.buf = self.buf[:self.cur] + self.buf[self.cur + 1:]
            elif key == "ENTER":
                line = self.buf
                if line and (not self.history or self.history[-1] != line):
                    self.history.append(line)
                    if len(self.history) > HISTORY_MAX:
                        del self.history[: -HISTORY_MAX]
                self.buf = ""
                self.cur = 0
                self.draw_prompt()
                return line
            elif key == "BS":
                if self.cur > 0:
                    self.buf = self.buf[:self.cur - 1] + self.buf[self.cur:]
                    self.cur -= 1
            elif key == "DEL":
                if self.cur < len(self.buf):
                    self.buf = self.buf[:self.cur] + self.buf[self.cur + 1:]
            elif key == "UP":
                if self.hist_idx > 0:
                    if self.hist_idx == len(self.history):
                        self.scratch = self.buf
                    self.hist_idx -= 1
                    self.buf = self.history[self.hist_idx]
                    self.cur = len(self.buf)
            elif key == "DOWN":
                if self.hist_idx < len(self.history):
                    self.hist_idx += 1
                    self.buf = (self.scratch if self.hist_idx == len(self.history)
                                else self.history[self.hist_idx])
                    self.cur = len(self.buf)
            elif key == "LEFT":
                self.cur = max(0, self.cur - 1)
            elif key == "RIGHT":
                self.cur = min(len(self.buf), self.cur + 1)
            elif key == "HOME":
                self.cur = 0
            elif key == "END":
                self.cur = len(self.buf)
            elif key == "KILL_LEFT":  # Ctrl-U
                self.buf = self.buf[self.cur:]
                self.cur = 0
            elif key == "KILL_RIGHT":  # Ctrl-K
                self.buf = self.buf[:self.cur]
            elif key == "REDRAW":  # Ctrl-L
                sys.stdout.write(_move(1, 1) + _CLEAR_BELOW)
                sys.stdout.flush()
            elif isinstance(key, str) and key:
                self.buf = self.buf[:self.cur] + key + self.buf[self.cur:]
                self.cur += len(key)

            self.draw_prompt()

    def _read_key(self) -> str | None:
        try:
            b = os.read(self.fd, 1)
        except (InterruptedError, BlockingIOError):
            return None
        if not b:
            return "EOF"
        ch = b[0]
        if ch == 0x03:
            return "INT"           # Ctrl-C
        if ch == 0x04:
            return "EOF"           # Ctrl-D
        if ch in (0x0d, 0x0a):
            return "ENTER"
        if ch in (0x7f, 0x08):
            return "BS"
        if ch == 0x01:
            return "HOME"          # Ctrl-A
        if ch == 0x05:
            return "END"           # Ctrl-E
        if ch == 0x0b:
            return "KILL_RIGHT"    # Ctrl-K
        if ch == 0x0c:
            return "REDRAW"        # Ctrl-L
        if ch == 0x15:
            return "KILL_LEFT"     # Ctrl-U
        if ch == 0x1b:  # ESC — possibly an escape sequence
            rdy, _, _ = select.select([self.fd], [], [], 0.05)
            if not rdy:
                return None
            b2 = os.read(self.fd, 1)
            if b2 != b"[":
                return None
            b3 = os.read(self.fd, 1)
            simple = {b"A": "UP", b"B": "DOWN", b"C": "RIGHT", b"D": "LEFT",
                      b"H": "HOME", b"F": "END"}
            if b3 in simple:
                return simple[b3]
            if b3 in (b"1", b"3", b"4", b"5", b"6", b"7", b"8"):
                # Eat the trailing '~' (and any modifier digits).
                while True:
                    rdy, _, _ = select.select([self.fd], [], [], 0.05)
                    if not rdy:
                        break
                    nxt = os.read(self.fd, 1)
                    if nxt == b"~":
                        break
                if b3 == b"3":
                    return "DEL"
                if b3 == b"1" or b3 == b"7":
                    return "HOME"
                if b3 == b"4" or b3 == b"8":
                    return "END"
            return None
        if ch < 0x20:
            return None  # other control chars: ignore
        # UTF-8 multi-byte sequence
        if ch >= 0x80:
            if (ch & 0xE0) == 0xC0:
                cnt = 1
            elif (ch & 0xF0) == 0xE0:
                cnt = 2
            elif (ch & 0xF8) == 0xF0:
                cnt = 3
            else:
                return None
            try:
                rest = os.read(self.fd, cnt)
                return (b + rest).decode("utf-8")
            except (OSError, UnicodeDecodeError):
                return None
        return b.decode("ascii", errors="ignore")


# ── REPL loop ────────────────────────────────────────────────────────────────
def banner(state: State) -> None:
    mode = "raw CAN" if state.raw else "ISOTP"
    log(c(f"cantalk {__version__}", BOLD) + c(f"  ·  {state.iface}  ·  {mode}", DIM))
    log(c("Type :help for commands, quit (or Ctrl-C / Ctrl-D) to exit.", DIM))
    if state.tx_id is None:
        hint = ":7DF,7E8 (ISOTP)" if not state.raw else ":7DF (raw)"
        log(c(f"Set arbitration first, e.g. {hint}.", DIM))
    log()


def repl(state: State, term: Term) -> int:
    while True:
        try:
            line = term.read_line()
        except (EOFError, KeyboardInterrupt):
            return 0

        line = line.strip()
        if not line:
            continue
        if line in ("quit", "exit", ":q", ":quit"):
            return 0
        if line.startswith(":"):
            handle_command(state, line[1:])
            continue
        if state.tx_id is None:
            hint = ":7DF,7E8" if not state.raw else ":7DF"
            log(f"  {c('!', BRIGHT_RED)} set arbitration first, e.g. {hint}")
            continue
        try:
            payload = parse_hex(line)
        except ValueError as e:
            log(f"  {c('!', BRIGHT_RED)} syntax: {e}")
            continue
        send_and_receive(state, payload)


# ── State persistence (history + per-iface arbitration) ─────────────────────
def _state_dir() -> str:
    base = os.environ.get("XDG_STATE_HOME") or os.path.expanduser("~/.local/state")
    return os.path.join(base, "cantalk")


def _history_path() -> str:
    return os.path.join(_state_dir(), "history")


def _settings_path() -> str:
    return os.path.join(_state_dir(), "state.json")


def _load_settings() -> dict:
    try:
        with open(_settings_path(), "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return {}


def _save_settings(data: dict) -> None:
    try:
        os.makedirs(_state_dir(), exist_ok=True)
        with open(_settings_path(), "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
            f.write("\n")
    except OSError:
        pass


def _restore_arbitration(state: State, iface_settings: dict) -> bool:
    """Apply saved tx/rx for this iface. Return True on success."""
    tx_str = iface_settings.get("tx")
    if not tx_str:
        return False
    try:
        tx = int(tx_str, 16)
        rx_str = iface_settings.get("rx")
        rx = int(rx_str, 16) if rx_str else None
    except (TypeError, ValueError):
        return False
    if rx is None and not state.raw:
        rx = derive_rx(tx)
    try:
        state.set_arbitration(tx, rx)
    except (OSError, ValueError):
        return False
    return True


def _load_history() -> list[str]:
    path = _history_path()
    try:
        with open(path, "r", encoding="utf-8") as f:
            return [ln.rstrip("\n") for ln in f if ln.strip()][-HISTORY_MAX:]
    except (FileNotFoundError, OSError):
        return []


def _save_history(hist: list[str]) -> None:
    if not hist:
        return
    path = _history_path()
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            for line in hist[-HISTORY_MAX:]:
                f.write(line + "\n")
    except OSError:
        pass


# ── Entry point ──────────────────────────────────────────────────────────────
def main() -> int:
    ap = argparse.ArgumentParser(prog="cantalk", add_help=False)
    ap.add_argument("interface", nargs="?")
    ap.add_argument("arbitration", nargs="?")
    ap.add_argument("--raw", action="store_true")
    ap.add_argument("-t", "--timeout", type=float, default=DEFAULT_TIMEOUT)
    ap.add_argument("-p", "--pad", "--padding", default=f"{DEFAULT_PADDING:02X}",
                    metavar="HEX",
                    help="pad CAN frames to 8 bytes with HEX byte "
                         "(default: AA; 'none' to disable)")
    ap.add_argument("--plain", action="store_true",
                    help="disable the bottom-anchored TUI; one prompt per line")
    ap.add_argument("--no-color", action="store_true")
    ap.add_argument("-V", "--version", action="store_true")
    ap.add_argument("-h", "--help", action="store_true")
    args = ap.parse_args()

    if args.no_color:
        common.set_color(False)

    if args.version:
        print(f"cantalk {__version__}")
        return 0

    if args.help or not args.interface:
        print(__doc__.strip())
        return 0 if args.help else 2

    padding: int | None
    pad_arg = (args.pad or "").strip()
    if pad_arg.lower() in ("none", "off", "no", ""):
        padding = None
    else:
        token = pad_arg[2:] if pad_arg.lower().startswith("0x") else pad_arg
        try:
            padding = int(token, 16) & 0xFF
        except ValueError:
            print(f"cantalk: bad --pad {args.pad!r}", file=sys.stderr)
            return 2

    state = State(args.interface, args.raw, args.timeout, padding)

    settings = _load_settings()
    restored_from_state = False

    if args.arbitration:
        parts = [p.strip() for p in args.arbitration.split(",")]
        if len(parts) > 2:
            print(f"cantalk: bad arbitration {args.arbitration!r}", file=sys.stderr)
            return 2
        try:
            tx = parse_id(parts[0])
            rx = parse_id(parts[1]) if len(parts) == 2 and parts[1] else None
        except ValueError as e:
            print(f"cantalk: bad arbitration {args.arbitration!r}: {e}", file=sys.stderr)
            return 2
        if rx is None and not args.raw:
            rx = derive_rx(tx)
        try:
            state.set_arbitration(tx, rx)
        except OSError as e:
            print(f"cantalk: cannot bind to {args.interface}: {e}", file=sys.stderr)
            return 1
    else:
        restored_from_state = _restore_arbitration(
            state, settings.get(args.interface, {})
        )

    history = _load_history()
    global _active_term
    rc = 0
    try:
        with Term(state, history, active=not args.plain) as term:
            _active_term = term
            banner(state)
            if restored_from_state and state.tx_id is not None:
                rx_str = (fmt_id(state.rx_id) if state.rx_id is not None else "*")
                log(c(
                    f"  · restored arbitration {fmt_id(state.tx_id)} → {rx_str} "
                    f"from previous session", DIM
                ))
            rc = repl(state, term)
    finally:
        _active_term = None
        # Persist arbitration for this iface (if one was set during the session).
        if state.tx_id is not None:
            settings[args.interface] = {
                "tx": fmt_id(state.tx_id),
                "rx": fmt_id(state.rx_id) if state.rx_id is not None else None,
            }
            _save_settings(settings)
        state.close_socket()
        _save_history(history)
    return rc


if __name__ == "__main__":
    sys.exit(main())
