#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.9"
# dependencies = ["pyserial"]
# ///
"""uboot-flash.py — write a new U-Boot to the appliance's SD over the serial console.

Runs on the Mac. No card removal, and no MMC driver needed in Linux: U-Boot has
its own, so this works even though the appliance's kernel has CONFIG_MMC=n.

Why this exists: the DRAM clock is compiled into the SPL, so every DRAM
experiment used to mean powering down, pulling the card, carrying it to the Mac,
bmaptool, and putting it back. This does the same job in about a minute.

    # at the U-Boot prompt already (see below for how to get there):
    uv run scripts/uboot-flash.py u-boot-sunxi-with-spl.bin

Getting a U-Boot prompt: CONFIG_BOOTDELAY is 0, but autoboot can still be
interrupted by spamming a key while the board powers up. Reset the board and
hold a key down; you want the `=>` prompt.

What it does, all over the one serial line:

    loady 0x42000000          # U-Boot waits for a Y-modem batch
    <this script sends the file>
    mmc dev 0
    mmc write 0x42000000 0x10 <blocks>

Sector 0x10 (8 KiB) is where sunxi looks for the SPL, and it matches the wic
layout (`part u-boot ... --align 8`). The FAT boot partition does not start until
sector 4096, so there is a ~2 MB window here and a 521 KB U-Boot is nowhere near
it. Nothing else on the card is touched — in particular the kernel is untouched,
so this only changes U-Boot-level settings (DRAM clock, watchdog, bootdelay).

Y-modem is implemented inline rather than shelling out to `sz`, so there is
nothing to `brew install`.
"""
import os
import sys
import time

import serial

SOH, STX, EOT, ACK, NAK, CAN, CRC = 0x01, 0x02, 0x04, 0x06, 0x15, 0x18, 0x43

DEV = os.environ.get("PI_DEV", "/dev/tty.usbserial-10")
LOAD_ADDR = os.environ.get("UB_ADDR", "0x42000000")
SECTOR = 512
SPL_SECTOR = 0x10  # sunxi SPL offset, 8 KiB

if len(sys.argv) < 2:
    sys.exit(__doc__)
path = sys.argv[1]
data = open(path, "rb").read()
blocks = (len(data) + SECTOR - 1) // SECTOR
print(f"{os.path.basename(path)}: {len(data)} bytes -> {blocks} sectors (0x{blocks:x})")

ser = serial.Serial(DEV, 115200, timeout=1)


def crc16(buf):
    crc = 0
    for b in buf:
        crc ^= b << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) & 0xFFFF if crc & 0x8000 else (crc << 1) & 0xFFFF
    return crc


def send_cmd(cmd, wait=1.0):
    ser.reset_input_buffer()
    ser.write((cmd + "\n").encode())
    ser.flush()
    time.sleep(wait)
    return ser.read(ser.in_waiting or 1).decode(errors="replace")


def wait_for(byte, timeout=15.0, label=""):
    """Wait for one specific control byte from the receiver."""
    end = time.time() + timeout
    while time.time() < end:
        d = ser.read(1)
        if d and d[0] == byte:
            return True
        if d and d[0] == CAN:
            sys.exit(f"receiver cancelled during {label}")
    return False


def send_block(seq, payload):
    """One Y-modem block, retried a few times on NAK."""
    head = STX if len(payload) > 128 else SOH
    frame = bytes([head, seq & 0xFF, (~seq) & 0xFF]) + payload
    frame += crc16(payload).to_bytes(2, "big")
    for attempt in range(10):
        ser.write(frame)
        ser.flush()
        end = time.time() + 10
        while time.time() < end:
            d = ser.read(1)
            if not d:
                continue
            if d[0] == ACK:
                return
            if d[0] in (NAK, CRC):
                break  # resend
            if d[0] == CAN:
                sys.exit("receiver cancelled")
    sys.exit(f"block {seq} not acknowledged after 10 attempts")


# --- hand U-Boot the loady command, then speak Y-modem at it -----------------
print(send_cmd("").strip()[-60:] or "(no prompt echo; assuming => prompt)")
ser.reset_input_buffer()
ser.write(f"loady {LOAD_ADDR}\n".encode())
ser.flush()

if not wait_for(CRC, timeout=15, label="loady handshake"):
    sys.exit("U-Boot never asked for a Y-modem transfer — are you at the `=>` prompt?")

# Block 0 carries the filename and size.
name = os.path.basename(path).encode()
hdr = name + b"\0" + str(len(data)).encode() + b"\0"
send_block(0, hdr.ljust(128, b"\0"))
if not wait_for(CRC, timeout=15, label="header ack"):
    sys.exit("receiver did not request data after the header block")

t0 = time.time()
seq = 1
for off in range(0, len(data), 1024):
    chunk = data[off:off + 1024]
    send_block(seq, chunk.ljust(1024, b"\x1a"))
    seq += 1
    if seq % 32 == 0 or off + 1024 >= len(data):
        done = min(off + 1024, len(data))
        rate = done / (time.time() - t0)
        print(f"  {done}/{len(data)} bytes ({rate:.0f} B/s)")

# End of file. Strict Y-modem says the receiver NAKs the first EOT and ACKs the
# second, but U-Boot's xyzModem does not always play that back the same way (it
# has been seen to answer with CAN). The transfer is already complete and
# verified by then, so treat anything here as good enough and just drain: the
# real check is the crc32 below, not the shape of this handshake.
ser.write(bytes([EOT]))
ser.flush()
time.sleep(0.3)
ser.read(ser.in_waiting or 1)
ser.write(bytes([EOT]))
ser.flush()
time.sleep(0.5)
ser.read(ser.in_waiting or 1)

time.sleep(1.5)
ser.read(ser.in_waiting or 1)

# --- verify RAM before touching the card -------------------------------------
# This is the real integrity check on the transfer. Do not skip it: a bad write
# to sector 0x10 leaves a board that cannot boot far enough to be reflashed this
# way, and the recovery is pulling the card.
import zlib
want = zlib.crc32(data) & 0xFFFFFFFF
out = send_cmd(f"crc32 {LOAD_ADDR} {len(data):x}", 3.0)
if f"{want:08x}" not in out.lower():
    sys.exit(f"CRC32 mismatch — RAM does not hold the file (wanted {want:08x}):\n{out}")
print(f"crc32 {want:08x} verified in RAM")

# --- commit it to the card ---------------------------------------------------
print(send_cmd("mmc dev 0", 1.5))
out = send_cmd(f"mmc write {LOAD_ADDR} {SPL_SECTOR:x} {blocks:x}", 3.0)
print(out)
if "OK" not in out and "written" not in out.lower():
    sys.exit("mmc write did not report success — do NOT power cycle; check the console")

# --- read it back off the card and check it -----------------------------------
# The RAM check above only proves the transfer was good; this proves the card
# actually holds what we think it does. Worth the extra two seconds: if a board
# then fails to boot, this is what tells you the setting is at fault rather than
# the flashing, and that distinction is otherwise very hard to make.
READBACK = "0x43000000"
send_cmd(f"mmc read {READBACK} {SPL_SECTOR:x} {blocks:x}", 3.0)
out = send_cmd(f"crc32 {READBACK} {len(data):x}", 3.0)
if f"{want:08x}" not in out.lower():
    sys.exit(f"READBACK MISMATCH — the card does not hold the file (wanted {want:08x}):\n"
             f"{out}\nDo NOT power cycle; reflash before resetting.")
print(f"readback from card verified ({want:08x})")
print("Written. Reset the board to run the new U-Boot.")
ser.close()
