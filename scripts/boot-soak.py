#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.9"
# dependencies = ["pyserial"]
# ///
"""boot-soak.py — warm-reboot the appliance N times and grade every boot.

Runs on the Mac. Each cycle reboots the board (from a U-Boot prompt or a
Linux console, whichever it finds), records the SPL DRAM banner and whether
Linux reached a login prompt or oopsed, and appends the raw console to a log.
Used for the 23-boot soak of the patched SPL on 2026-09-08 (DEVELOPMENT.md §9).

    uv run scripts/boot-soak.py            # 20 boots, log in boot-soak.log
    uv run scripts/boot-soak.py 5 soak.log

The summary counts a boot as good only when the banner says 256 MiB AND login
was reached; a 512 MiB banner is the SPL misdetection and nothing after it
means anything. Stops early on a boot that never reaches login (a hung kernel
needs a power-cycle). PI_DEV overrides the serial node.
"""
import sys
import time

import applib as A

N = int(sys.argv[1]) if len(sys.argv) > 1 else 20
LOG = sys.argv[2] if len(sys.argv) > 2 else "boot-soak.log"

ser = A.open_serial(timeout=0.05)
log = open(LOG, "ab")


def kick():
    """Trigger a reboot from wherever the console is."""
    ser.write(b"\n")
    ser.flush()
    tail = A.drain(ser, 1.0)[-300:]
    if "=>" in tail:
        ser.write(b"reset\n")
    else:
        ser.write(b"root\n")
        ser.flush()
        A.read_until(ser, [b"# "], 6.0, log=log)
        time.sleep(0.3)
        ser.write(b"reboot -f\n")
    ser.flush()


results = []
for i in range(1, N + 1):
    kick()
    text = A.watch_boot(ser, timeout=40, log=log)
    r = A.boot_report(text)
    dram = r["dram_mib"] or "?"
    if r["oops"]:
        state = "OOPS " + r.get("oops_at", "?")
    elif r["login"]:
        state = "login"
    else:
        state = "timeout"
    results.append((dram, state))
    print(f"boot {i:2d}: DRAM {dram} MiB, {state}", flush=True)
    if state == "timeout":
        break
    if state.startswith("OOPS"):
        A.drain(ser, 15)  # let the panic reboot happen before kicking again

good = sum(1 for d, s in results if d == "256" and s == "login")
print(f"summary: {good}/{len(results)} boots printed 256 MiB and reached login")
sys.exit(0 if good == len(results) else 1)
