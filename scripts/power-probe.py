#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.9"
# dependencies = ["pyserial"]
# ///
"""power-probe.py — measure how busy the appliance actually is, over the serial console.

Runs on the Mac. Takes two snapshots of /proc/uptime, /proc/interrupts and the
cpufreq/hotplug state, `interval` seconds apart, and reports the deltas:

    wakeups/s   timer interrupts per second summed over the online cores
    busy%       CPU time not spent idle, as a fraction of (interval x online cores)

Those two numbers are the cheap stand-in for a wattmeter: on this board almost
all avoidable power is avoidable wakeups, and they can be sampled in 10 s
without touching the meter. Take the meter reading for the headline number and
use this for the A/B iteration in between.

Usage (PI_DEV overrides the serial node, as in pi-serial.py):

    uv run scripts/power-probe.py            # 10 s sample
    uv run scripts/power-probe.py 30         # 30 s sample
    PI_DEV=/dev/tty.usbserial-XXXX uv run scripts/power-probe.py

Reference measurements, appliance idle with a watch attached and enumerated
(2026-08-06, usb-proxy 56aa36a): 1962 wakeups/s, 3.3% busy. Essentially all of
it was ep_loop_write's 1 ms polled condvar wait, two endpoints x 1000/s.

This shells out to pi-serial.py rather than reimplementing the login dance, so
the whole sample is a single serial session.
"""
import os
import pathlib
import subprocess
import sys

interval = float(sys.argv[1]) if len(sys.argv) > 1 else 10.0
here = pathlib.Path(__file__).resolve().parent

# One command, two snapshots. Kept well under the 1024-char busybox ash line
# editing limit. The markers are matched as whole lines, so the console's echo
# of this command line (which contains them all) does not confuse the parser.
SNAP = (
    "cat /proc/uptime;"
    "cat /sys/devices/system/cpu/online;"
    "cat /sys/devices/system/cpu/cpufreq/policy0/scaling_cur_freq;"
    "cat /sys/devices/system/cpu/cpufreq/policy0/scaling_governor;"
    "grep -E 'arch_timer|ehci|ohci|musb|mmc|IPI' /proc/interrupts"
)
cmd = f"snap(){{ {SNAP}; }}; echo ==A; snap; sleep {interval:g}; echo ==B; snap; echo ==END"

out = subprocess.run(
    ["uv", "run", str(here / "pi-serial.py"), cmd, str(interval + 6)],
    capture_output=True, text=True, cwd=here.parent,
    env=os.environ,
).stdout

lines = [l.rstrip() for l in out.splitlines()]
try:
    a = lines.index("==A")
    b = lines.index("==B")
    end = lines.index("==END")
except ValueError:
    sys.exit(f"could not find markers in console output; got:\n{out}")


def parse(block):
    """(uptime, idle, online, kHz, governor, {irq_label: total_across_cpus})."""
    up, idle = (float(x) for x in block[0].split())
    online, khz, gov = block[1], int(block[2]), block[3]
    irqs = {}
    for line in block[4:]:
        label, _, rest = line.partition(":")
        fields = rest.split()
        counts = []
        for f in fields:
            if not f.lstrip("-").isdigit():
                break
            counts.append(int(f))
        if counts:
            # IRQ lines repeat the number in the description ("GICv2 30 Level");
            # only the leading per-CPU columns are counts, and the break above
            # stops at the first non-numeric field ("GICv2" / "CPU").
            irqs[f"{label.strip()} {' '.join(fields[len(counts):])}".strip()] = sum(counts)
    return up, idle, online, khz, gov, irqs


up0, idle0, online0, khz0, gov0, irq0 = parse(lines[a + 1:b])
up1, idle1, online1, khz1, gov1, irq1 = parse(lines[b + 1:end])


def ncpu(spec):
    n = 0
    for part in spec.split(","):
        if "-" in part:
            lo, hi = part.split("-")
            n += int(hi) - int(lo) + 1
        else:
            n += 1
    return n


wall = up1 - up0
cores = ncpu(online1)
if wall <= 0:
    sys.exit(f"bad uptime delta ({wall}); the sample did not complete")
if online0 != online1:
    print(f"! online cores changed mid-sample ({online0} -> {online1}); busy% is approximate")

# /proc/uptime's second field is idle time summed over the online CPUs.
busy = 100.0 * (1.0 - (idle1 - idle0) / (wall * cores))
timer = sum(v1 - irq0.get(k, 0) for k, v1 in irq1.items() if "arch_timer" in k)

print(f"\nsample {wall:.2f}s | cores {online1} ({cores}) | {khz1 // 1000} MHz | {gov1}")
print(f"wakeups/s {timer / wall:8.1f}   busy% {busy:6.2f}\n")

print(f"{'interrupt':<34}{'delta':>8}{'per sec':>10}")
for k, v1 in irq1.items():
    d = v1 - irq0.get(k, 0)
    if d:
        print(f"{k:<34}{d:>8}{d / wall:>10.1f}")
