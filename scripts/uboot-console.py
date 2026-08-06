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

PI_DEV overrides the serial node, as everywhere else in this repo.
"""
import os
import sys
import time

import serial

DEV = os.environ.get("PI_DEV", "/dev/tty.usbserial-10")

args = sys.argv[1:]
catch = "--catch" in args
args = [a for a in args if a != "--catch"]
cmd = args[0] if args else ""
secs = float(args[1]) if len(args) > 1 else 3.0

if not catch and not cmd:
    sys.exit(__doc__)

ser = serial.Serial(DEV, 115200, timeout=0.05)


def drain(seconds):
    buf = b""
    end = time.time() + seconds
    while time.time() < end:
        d = ser.read(ser.in_waiting or 1)
        if d:
            buf += d
    return buf.decode(errors="replace")


if catch:
    # Kick Linux over. Works whether the console sits at a login prompt or a
    # shell: the login prompt simply ignores the command.
    ser.write(b"\n")
    time.sleep(0.3)
    ser.write(b"root\n")
    time.sleep(0.8)
    ser.write(b"reboot -f\n")
    ser.flush()

    buf = b""
    end = time.time() + 30
    got = False
    while time.time() < end:
        ser.write(b"a")  # any printable char breaks autoboot
        d = ser.read(ser.in_waiting or 1)
        if d:
            buf += d
            if b"=>" in buf[-200:]:
                got = True
                break
        time.sleep(0.004)

    time.sleep(0.5)
    ser.write(b"\x03")  # clear the line of accumulated spam
    ser.flush()
    time.sleep(0.5)
    ser.reset_input_buffer()

    if not got:
        ser.close()
        sys.exit("did not reach the `=>` prompt within 30s")
    print("at U-Boot prompt")

if cmd:
    ser.reset_input_buffer()
    ser.write((cmd + "\n").encode())
    ser.flush()
    sys.stdout.write(drain(secs))

ser.close()
