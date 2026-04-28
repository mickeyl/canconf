# Changelog

All notable changes to **canconf** are recorded here, newest first. Patch
releases that only adjusted packaging or release plumbing are folded into the
next user-visible release.

## v0.4.1 — 2026-04-28

- `canmon`: per-interface parameters (`sp`, `qlen`, `drv`) are now printed
  exactly once, on the iface's first present snapshot. Previous releases
  re-emitted them every time an interface disappeared and came back, mixed in
  with `STATE` / `CONFIG` notes.
- `canmon`: the day-change ruler now carries a centred `New Day: <weekday
  YYYY-MM-DD>` label so long-running sessions stay readable.

## v0.4.0 — 2026-04-26

- New tool **`cantalk`** — a minimal interactive REPL for talking to an ECU.
  ISOTP via the kernel `can-isotp` module by default (segmentation, flow
  control, and reassembly handled in-kernel); `--raw` drops to single CAN
  frames. `:TX[,RX]` sets arbitration, bare hex sends, `quit` / Ctrl-D exits.
  Default RX derivation is OBD2-style (TX+8) for 11-bit IDs and J1939-style
  source/target swap for 29-bit `18DA<target><source>` IDs.

## v0.3.4 — 2026-04-25

- Refactored: the `canconf status` table is now produced by a shared
  `common.status_lines()` helper, so `canconf` and `canmon` share one
  formatter.

## v0.3.3 — 2026-04-25

- `canmon`: auto-discovers CAN interfaces that appear after startup
  (hot-plugged USB-CAN adapters no longer require restarting the monitor).
- `canmon`: distinguishes `MISSING` (iface absent from `ip link show`) from
  `NO-CAN-DATA` (link exists but no CAN details exposed) and shows both
  explicitly.
- `canmon`: gained a header row and a separator that breaks output across
  day boundaries.

## v0.3.2 — 2026-04-22

- `canconf`: bare bitrate specs (e.g. `canconf 500k`) now emit `fd off`
  explicitly. Previously, reconfiguring an iface that had been in CAN-FD
  mode left it in FD with the old data bitrate, because the kernel only
  touches `ctrlmode` bits that appear in the netlink message.
- `canconf`: rates below 10 kbit/s are rejected as probable typos
  (`canconf 800` → suggests `canconf 800k`).
- `canconf`: per-iface failures during reconfigure no longer abort the whole
  run; failed ifaces are reported by name and skipped for subsequent steps,
  and a mixed-state warning is printed when ifaces sharing a bus end up
  configured differently.

## v0.3.0 — 2026-04-22

First broadly useful release.

- `canconf` reconfigures all CAN / CAN-FD interfaces in one shot from a terse
  spec (`500k`, `500k/2M`, `500k/2M@0.875/0.75`, `up`, `off`).
- `canconf bitrates` derives the achievable bitrate envelope from
  `bittiming_const` + `clock` and intersects it with the standard CAN bitrate
  set, separately for nominal and FD data phases.
- `canconf status` lists each interface with state, mode, bitrate, sample
  point, txqueuelen, and SocketCAN driver name.
- New tool **`canmon`** — read-only live health monitor. Tails state,
  bittiming, frame-error deltas, and controller bit-error deltas per
  interface per tick; flags state transitions, config changes, auto-restarts,
  and threshold breaches. Reads kernel stats only — no CAN socket, no root.
- Colorized output across both tools.
