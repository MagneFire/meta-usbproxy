#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.9"
# dependencies = ["pyserial"]
# ///
"""uboot-console.py — talk to the appliance's U-Boot prompt over the serial console.

This is pi-serial.py's counterpart for U-Boot. Do not use pi-serial.py here: it
does a Linux getty login dance and sends Ctrl-U, neither of which means anything
to U-Boot.

    uv run scripts/uboot-console.py --catch          # reboot and land at `=>`
    uv run scripts/uboot-console.py "mmc info"       # run a command, print reply
    uv run scripts/uboot-console.py --catch "version"  # both, in one go

--catch exists because CONFIG_BOOTDELAY is 0. That does *not* prevent reaching
the prompt: autoboot is still interruptible by spamming a key while the board
comes up, so --catch tells Linux to reboot and then hammers the line until `=>`
appears. (An earlier version of these notes claimed bootdelay had to be raised to
1 to get a prompt. It does not.)

Typical use is a DRAM-clock experiment, since the clock is compiled into the SPL:

    uv run scripts/uboot-console.py --catch
    uv run scripts/uboot-flash.py u-boot-sunxi-with-spl.bin
    uv run scripts/uboot-console.py "reset"

PI_DEV overrides the serial node, as everywhere else in this repo; with one
dongle plugged in it is found automatically.
"""
import sys
import time

import applib as A

args = sys.argv[1:]
catch = "--catch" in args
args = [a for a in args if a != "--catch"]
cmd = args[0] if args else ""
secs = float(args[1]) if len(args) > 1 else 3.0

if not catch and not cmd:
    sys.exit(__doc__)

ser = A.open_serial(timeout=0.05)

if catch:
    if not A.ub_catch_prompt(ser):
        ser.close()
        sys.exit("did not reach the `=>` prompt within 30s")
    print("at U-Boot prompt")

if cmd:
    ser.reset_input_buffer()
    ser.write((cmd + "\n").encode())
    ser.flush()
    sys.stdout.write(A.drain(ser, secs))

ser.close()
