#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.9"
# dependencies = ["pyserial"]
# ///
"""boot-soak.py — reboot (or power-cycle) the appliance N times and grade every boot.

Runs on the Mac. Each cycle restarts the board, records the SPL DRAM banner
and whether Linux reached a login prompt or oopsed, and appends the raw
console to a log. After an oops it logs in, saves the pstore records next to
the log and clears them, so the crash text is never lost to the next reboot.
Used for the 23-boot soak of the patched SPL on 2026-09-08 (DEVELOPMENT.md §9).

    uv run scripts/boot-soak.py                 # 20 warm reboots, boot-soak.log
    uv run scripts/boot-soak.py 100 soak.log    # warm, 100 boots
    uv run scripts/boot-soak.py 20 --cold       # you cut the power each time
    uv run scripts/boot-soak.py 20 --cold --power-cmd 'uhubctl -l 1-1 -p 2 -a cycle'

Warm: `reboot -f` from Linux or `reset` from U-Boot, whichever prompt is
there. The SoC resets but the DRAM chip keeps its state, so this never
exercises a cold DRAM init or a supply ramp.

Cold (--cold): the script asks for a power-cycle and waits for the SPL banner
(up to --cold-timeout s), or runs --power-cmd and waits. That is the only way
to test the cold DRAM init and the rails coming up under boot load.

The summary counts a boot as good only when the banner says 256 MiB AND login
was reached; a 512 MiB banner is the SPL misdetection and nothing after it
means anything. Stops early on a boot that never reaches login (a hung kernel
needs a power-cycle). PI_DEV overrides the serial node. Exit 0 only when every
boot was good.
"""
import argparse
import pathlib
import subprocess
import sys
import time

import applib as A

ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
ap.add_argument("n", type=int, nargs="?", default=20, help="boots (default 20)")
ap.add_argument("log", nargs="?", default="boot-soak.log", help="raw console log (appended)")
ap.add_argument("--cold", action="store_true", help="power-cycle instead of reboot")
ap.add_argument("--power-cmd", help="shell command that cycles the supply (cold mode); "
                "without it the script prompts you")
ap.add_argument("--cold-timeout", type=float, default=600, help="seconds to wait for the SPL banner "
                "after asking for a power-cycle (default 600)")
args = ap.parse_args()

N = args.n
LOG = pathlib.Path(args.log)
ser = A.open_serial(timeout=0.05)
log = open(LOG, "ab")


def kick_warm():
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


def kick_cold(i):
    """Get the board power-cycled, then wait for the SPL to speak."""
    ser.reset_input_buffer()
    if args.power_cmd:
        r = subprocess.run(args.power_cmd, shell=True)
        if r.returncode:
            sys.exit(f"--power-cmd failed (exit {r.returncode})")
    else:
        print(f"boot {i:2d}: power-cycle the board now (unplug, wait 5 s, plug in)", flush=True)
    text, hit = A.read_until(ser, [b"U-Boot SPL", b"DRAM:"], args.cold_timeout, log=log)
    if hit is None:
        sys.exit("no SPL banner after the power-cycle; is the console dongle on the right node?")


def save_pstore(i):
    """After an oops-reboot: pull the ramoops records off the board, clear them."""
    if not A.lx_login(ser):
        print("   (could not log in to read pstore)", flush=True)
        return
    names = A.lx_run(ser, "ls /sys/fs/pstore", timeout=5.0) or ""
    recs = [n for n in names.split() if n.startswith("dmesg-ramoops")]
    if not recs:
        print("   (no dmesg-ramoops record in pstore)", flush=True)
        return
    out = LOG.with_name(f"{LOG.stem}-oops-{i}.txt")
    with open(out, "w") as f:
        for n in recs:
            body = A.lx_run(ser, f"cat /sys/fs/pstore/{n}", timeout=30.0) or ""
            f.write(f"===== {n}\n{body}\n")
    A.lx_run(ser, "rm -f /sys/fs/pstore/dmesg-ramoops-*", timeout=5.0)
    print(f"   pstore saved to {out} and cleared", flush=True)


results = []
for i in range(1, N + 1):
    if args.cold:
        kick_cold(i)
    else:
        kick_warm()
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
        # panic=10: the board reboots itself; follow that boot, then keep the record.
        text = A.watch_boot(ser, timeout=60, log=log)
        if A.boot_report(text)["login"]:
            save_pstore(i)
        else:
            print("   (board did not come back to login after the panic reboot)", flush=True)
            break

good = sum(1 for d, s in results if d == "256" and s == "login")
oops = [i + 1 for i, (d, s) in enumerate(results) if s.startswith("OOPS")]
bad_dram = [i + 1 for i, (d, s) in enumerate(results) if d != "256"]
mode = "cold power-ons" if args.cold else "warm reboots"
print(f"summary: {good}/{len(results)} {mode} printed 256 MiB and reached login"
      + (f"; oops on boot {oops}" if oops else "")
      + (f"; DRAM banner wrong on boot {bad_dram}" if bad_dram else ""))
sys.exit(0 if good == len(results) else 1)
