#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.9"
# dependencies = ["pyserial"]
# ///
"""
pi-serial.py — drive the Orange Pi Zero appliance's USB-serial console FROM macOS.

Runs on the Mac (the host machine), NOT inside the build container. It logs in as
root (empty password) over the USB-UART dongle, runs a command, and prints the
output. The login is idempotent: it works whether the console is sitting at a
login prompt or already at a shell.

Usage (via uv — pyserial is declared inline above, so uv installs it into an
ephemeral environment; nothing to pip-install):

    uv run scripts/pi-serial.py "<command>" [read_seconds]
    uv run scripts/pi-serial.py "cat /var/volatile/log/usb-proxy.log"
    uv run scripts/pi-serial.py "uptime" 6

If the file is executable you can also run it directly (the shebang invokes uv):

    ./scripts/pi-serial.py "<command>"

The serial device node varies between dongles/reconnects (it has been
/dev/tty.usbserial-10 and /dev/tty.usbserial-11410). Find it with
`ls /dev/tty.usbserial-*` and override the default via the PI_DEV env var:

    PI_DEV=/dev/tty.usbserial-XXXX uv run scripts/pi-serial.py "<command>"

Tip: to drop a file onto the appliance without paste/quoting trouble, base64 it
on the Mac and decode it on the Pi (no nested quotes):

    b64=$(base64 < some.json)
    uv run scripts/pi-serial.py "echo $b64 | base64 -d > /etc/usb-proxy/config.json"
"""
import os
import sys
import time

import serial

DEV = os.environ.get("PI_DEV", "/dev/tty.usbserial-10")
cmd = sys.argv[1] if len(sys.argv) > 1 else ""
secs = float(sys.argv[2]) if len(sys.argv) > 2 else 4.0

ser = serial.Serial(DEV, 115200, timeout=0.2)
buf = ""


def rd(t):
    global buf
    end = time.time() + t
    while time.time() < end:
        d = ser.read(4096)
        if d:
            buf += d.decode(errors="replace")


def send(s):
    ser.write(s.encode())
    ser.flush()


# Nudge the console; log in if a prompt appears.
send("\n")
rd(1.0)
if "login:" in buf[-300:] or "incorrect" in buf[-300:]:
    send("root\n")
    rd(1.5)
    if "assword" in buf[-120:]:
        send("\n")
        rd(1.5)

# Clear any partial input line, drain pending output, then run the command.
send("\x15")  # Ctrl-U
rd(0.3)
ser.reset_input_buffer()
buf = ""
if cmd:
    send(cmd + "\n")
rd(secs)
sys.stdout.write(buf)
ser.close()
