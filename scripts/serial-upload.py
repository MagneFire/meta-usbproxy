#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.9"
# dependencies = ["pyserial"]
# ///
"""Upload a small binary file to the Orange Pi appliance over the serial console.

Encodes the file as printf octal-escape chunks (busybox has no base64/uudecode)
and appends them to a target path on the Pi, then prints the Pi-side md5sum for
verification against the local md5 printed at the start. Runs on the Mac, like
pi-serial.py (PI_DEV env var overrides the serial device node).

Usage:
    PI_DEV=/dev/tty.usbserial-XXXX uv run scripts/serial-upload.py <local_file> <remote_path>

Throughput is ~0.5 KB/s of payload (~80 s for a 40 KB file), so gzip first.
The main use case is swapping in a test usb-proxy binary without reflashing
the SD card (the rootfs is RAM-backed, so this does not survive a reboot):

    # on the build host: stripped + gzipped binary
    arm-poky-linux-gnueabi-strip usb-proxy && gzip -9 usb-proxy
    # upload and install
    uv run scripts/serial-upload.py usb-proxy.gz /tmp/up.gz
    uv run scripts/pi-serial.py \
        "gunzip -f /tmp/up.gz && chmod +x /tmp/up && mv /tmp/up /usr/bin/usb-proxy \
         && kill -9 \$(pidof usb-proxy)" 8   # inittab respawns the new binary
"""
import hashlib
import os
import sys
import time

import serial

DEV = os.environ.get("PI_DEV", "/dev/tty.usbserial-10")
local, remote = sys.argv[1], sys.argv[2]
CHUNK = 225  # busybox ash line editing silently truncates input at 1024 chars
             # (CONFIG_FEATURE_EDITING_MAX_LEN); 225*4 escapes + ~35 overhead
             # keeps the whole command safely under that.

data = open(local, "rb").read()
print(f"local size={len(data)} md5={hashlib.md5(data).hexdigest()}")

ser = serial.Serial(DEV, 115200, timeout=0.2)
buf = ""


def rd(t):
    global buf
    end = time.time() + t
    while time.time() < end:
        d = ser.read(4096)
        if d:
            buf += d.decode(errors="replace")


def rd_some():
    """Read whatever has arrived, blocking at most one serial timeout.

    ser.read(4096) does NOT return early on partial data — it waits for all
    4096 bytes or the full timeout, which cost a flat 200ms on every chunk
    round trip. Reading in_waiting returns as soon as the reply lands.
    """
    return ser.read(ser.in_waiting or 1)


def send(s, paced=False):
    b = s.encode()
    if not paced:
        ser.write(b)
        ser.flush()
        return
    for i in range(0, len(b), 64):
        ser.write(b[i:i + 64])
        ser.flush()
        time.sleep(0.008)


def run(cmd, marker, timeout=8.0):
    """Send cmd, wait until marker appears in output."""
    global buf
    buf = ""
    ser.reset_input_buffer()
    send(cmd + "\n", paced=True)
    end = time.time() + timeout
    while time.time() < end:
        d = rd_some()
        if d:
            buf += d.decode(errors="replace")
            if marker in buf:
                return True
    return False


# Log in if needed.
send("\n")
rd(1.0)
if "login:" in buf[-300:] or "incorrect" in buf[-300:]:
    send("root\n")
    rd(1.5)
    if "assword" in buf[-120:]:
        send("\n")
        rd(1.5)
# Clear any stuck continuation prompt / partial line from a previous attempt:
# close a possibly-open single-quoted string, interrupt, clear the line, then
# prove we're at a clean prompt before sending any file data.
send("'\n")
rd(0.5)
send("\x03")
rd(0.5)
send("\x15\n")
rd(0.5)
if not run("echo SANITY-$((20+3))", "SANITY-23"):
    sys.exit(f"console not at a clean prompt:\n{buf[-500:]}")

if not run(f"rm -f {remote}; echo K$?", "K0"):
    sys.exit("failed to reset remote file")

# Stop the console echoing every command back at us. Each chunk is a ~935 char
# printf line, so the echo was doubling the bytes on the wire and adding a full
# line time to every round trip. Nothing below parses the echo — run() waits on
# the `echo K$?` reply, which is real output and still arrives.
run("stty -echo; echo K$?", "K0")

n = (len(data) + CHUNK - 1) // CHUNK
t0 = time.time()
try:
    for i in range(n):
        piece = data[i * CHUNK:(i + 1) * CHUNK]
        esc = "".join(f"\\{b:03o}" for b in piece)
        ok = run(f"printf '{esc}' >> {remote} && echo K$?", "K0")
        if not ok:
            sys.exit(f"chunk {i}/{n} failed:\n{buf[-500:]}")
        if (i + 1) % 10 == 0 or i + 1 == n:
            rate = (i + 1) * CHUNK / (time.time() - t0)
            print(f"chunk {i+1}/{n} ({time.time()-t0:.0f}s, {rate:.0f} B/s)")
finally:
    # Always hand the console back with echo on, including on a failed chunk —
    # otherwise the next interactive session looks dead.
    run("stty echo; echo K$?", "K0", timeout=4.0)

buf = ""
run(f"md5sum {remote}", remote, timeout=10)
print(buf)
ser.close()
