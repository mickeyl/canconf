# canconf — one-shot reconfiguration of all SocketCAN interfaces

`canconf` reconfigures every CAN / CAN-FD interface on a Linux host in a single,
terse command. It replaces this dance:

```bash
sudo ip link set can0 down
sudo ip link set can1 down
sudo ip link set can0 type can bitrate 500000 dbitrate 2000000 sample-point 0.875 dsample-point 0.75 fd on
sudo ip link set can1 type can bitrate 500000 dbitrate 2000000 sample-point 0.875 dsample-point 0.75 fd on
sudo ip link set can0 txqueuelen 10000
sudo ip link set can1 txqueuelen 10000
sudo ip link set can0 up
sudo ip link set can1 up
```

with this:

```bash
canconf 500k/2M@0.875/0.75
```

## Features

- Discovers all CAN interfaces automatically (any kernel net device with `ARPHRD_CAN`, i.e. `can*`, `vcan*`, `slcan*`).
- Classic CAN and CAN-FD with a single compact `BITRATE[/DBITRATE][@SP[/DSP]]` spec.
- Human-friendly bitrate suffixes: `125k`, `500k`, `1M`, `2M`, …
- Brings interfaces down, reconfigures, and brings them back up — atomically, per run.
- Prints the actual post-apply state (the driver may round bitrate to the nearest achievable value — you want to see that).
- Sets `txqueuelen 10000` by default (the kernel default of 10 is far too low for CAN).
- Self-elevates to root via `sudo` if not already root.
- Zero runtime dependencies — pure Python 3.9+ stdlib.

## Installation

### pipx (recommended)

```bash
pipx install canconf
```

### pip

```bash
pip install canconf
```

### From source

```bash
git clone https://github.com/mickeyl/canconf
cd canconf
pipx install .
```

## Usage

```
canconf                         show status of all can* interfaces
canconf 500k                    classic CAN @ 500 kbit/s, all interfaces, up
canconf 500k/2M                 CAN-FD: nominal 500k, data 2M
canconf 500k/2M@0.875/0.75      same, with nominal & data sample points
canconf off    |    down        bring all interfaces down
canconf up                      bring all interfaces up (no reconfigure)
```

### Options

| Flag | Description |
|------|-------------|
| `-i`, `--ifaces a,b,c` | Restrict to these interfaces (default: all CAN interfaces) |
| `-r`, `--restart-ms N` | Auto-restart on bus-off after N ms |
| `--listen-only` | Listen-only mode |
| `--loopback` | Loopback mode |
| `--one-shot` | One-shot mode |
| `--berr` | Enable bus error reporting |
| `--term OHM` | Set termination resistor (if the hardware supports it) |
| `--txqueuelen N` | Override the tx queue length (default: 10000) |
| `-n`, `--dry-run` | Print the `ip` commands that would run, do nothing |
| `-v`, `--verbose` | Print each `ip` command as it runs |
| `-q`, `--quiet` | Suppress the post-apply status dump |
| `-V`, `--version` | Print version and exit |
| `-h`, `--help` | Show help |

### Example session

```
❯ canconf
can0  UP  CAN  500k  sp 0.875
can1  UP  CAN  500k  sp 0.875

❯ canconf 500k/2M@0.875/0.75
[sudo] password for mickey: 
can0  UP  CAN-FD  500k / 2M  sp 0.875/0.750
can1  UP  CAN-FD  500k / 2M  sp 0.875/0.750

❯ canconf -n 1M -i can0
+ ip link set can0 down
+ ip link set can0 type can bitrate 1000000
+ ip link set can0 txqueuelen 10000
+ ip link set can0 up
```

## Why

Managing two or more physical CAN interfaces that are wired onto the same bus
means every parameter change has to be mirrored across all of them. The `ip
link` incantations for CAN-FD are long, order-sensitive, and easy to typo.
`canconf` makes the common cases trivial and keeps your interfaces in lock-step.

## License

MIT — see [LICENSE](LICENSE).
