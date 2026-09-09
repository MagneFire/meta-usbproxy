#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.9"
# dependencies = ["pyserial"]
# ///
"""uboot-flash.py — put a file on the appliance through the U-Boot serial console.

Runs on the Mac. No card removal, and no MMC driver needed in Linux: U-Boot has
its own, so this works even though the appliance's kernel has CONFIG_MMC=n.

    # Update U-Boot itself (SPL + U-Boot proper, raw sector 0x10):
    uv run scripts/uboot-flash.py --catch u-boot-sunxi-with-spl.bin

    # Update one file in the FAT boot partition (kernel or DTB):
    uv run scripts/uboot-flash.py --catch --fat uImage uImage-initramfs-orange-pi-zero.bin
    uv run scripts/uboot-flash.py --catch --fat sun8i-h2-plus-orangepi-zero.dtb board.dtb

    # Boot a kernel from RAM without writing the card at all (RAM-only test of
    # a whole image; the card's own kernel comes back on the next reboot):
    uv run scripts/uboot-flash.py --catch --boot uImage-initramfs-orange-pi-zero.bin

    # Faster transfers (43 KB/s instead of 9), U-Boot built with
    # CONFIG_SYS_LOADS_BAUD_CHANGE; 750000 is the rate the UART divides exactly:
    uv run scripts/uboot-flash.py --catch --baud 750000 --fat uImage ...

The kernel file to send is always the `uImage-initramfs-*.bin` one (~6 MB, the
rootfs is inside it). The plain `uImage-*.bin` next to it in the deploy
directory is the kernel alone; flashed as /uImage it hangs at "Starting
kernel" with no init and needs a power-cycle. This script refuses a kernel file
without "initramfs" in its name unless --allow-plain-kernel is given.

Getting a U-Boot prompt: CONFIG_BOOTDELAY is 0, but autoboot can still be
interrupted by spamming a key while the board comes up. --catch reboots the
board (from Linux or from U-Boot) and catches the prompt in the same serial
session. Without --catch the script confirms it is at `=>` before sending
anything and aborts if it is not, so a missed catch never runs blind against
the card.

What it does, all over the one serial line:

    loady <addr>              # U-Boot receives a Y-modem batch (sent inline, no lrzsz)
    crc32 <addr> <len>        # the transfer is verified in RAM first, always
    # then one of:
    mmc write <addr> 0x10 <blocks>; mmc read ...; crc32     (U-Boot)
    fatwrite mmc 0:1 <addr> <name> <len>; fatload ...; crc32 (--fat)
    fatload mmc 0:1 ${fdt_addr_r} ${fdtfile}; bootm <addr> - ${fdt_addr_r} (--boot)

Sector 0x10 (8 KiB) is where sunxi looks for the SPL and matches the wic layout
(`part u-boot ... --align 8`); the FAT partition starts at sector 4096, so a
~520 KB U-Boot is nowhere near it. --fat and --boot never touch the SPL.
--boot uses the bootargs from meta-sunxi's boot.cmd (the kernel is built with
CMDLINE_EXTEND, so they matter) and then follows the console until login.

PI_DEV overrides the serial node; otherwise the single /dev/tty.usbserial-*
present is used.
"""
import os
import sys
import time

import applib as A

SECTOR = 512
SPL_SECTOR = 0x10  # sunxi SPL offset, 8 KiB
LOAD_ADDR = os.environ.get("UB_ADDR", "0x42000000")   # == ${kernel_addr_r} here
READBACK = "0x43000000"

# The bootargs meta-sunxi's boot.scr sets. root= is meaningless with a bundled
# initramfs but harmless, and keeping the line identical means a RAM boot
# tests the same cmdline the card boots with.
BOOTARGS = "console=${console} console=tty1 root=/dev/mmcblk0p2 rootwait panic=10 ${extra}"


def usage():
    sys.exit(__doc__)


argv = sys.argv[1:]
catch = "--catch" in argv
allow_plain = "--allow-plain-kernel" in argv
argv = [a for a in argv if a not in ("--catch", "--allow-plain-kernel")]
baud = None
if "--baud" in argv:
    i = argv.index("--baud")
    try:
        baud = int(argv[i + 1])
    except (IndexError, ValueError):
        usage()
    del argv[i:i + 2]

mode = "uboot"
if len(argv) == 3 and argv[0] == "--fat":
    mode, fat_dest, path = "fat", argv[1], argv[2]
elif len(argv) == 2 and argv[0] == "--boot":
    mode, fat_dest, path = "boot", None, argv[1]
elif len(argv) == 1:
    fat_dest, path = None, argv[0]
else:
    usage()

data = open(path, "rb").read()
blocks = (len(data) + SECTOR - 1) // SECTOR
base = os.path.basename(path)

is_kernel = mode == "boot" or (mode == "fat" and fat_dest == "uImage")
if is_kernel and "initramfs" not in base and not allow_plain:
    sys.exit(f"{base}: a kernel without 'initramfs' in its name is the kernel-only "
             "build and hangs at 'Starting kernel' (no init). Send the "
             "uImage-initramfs-*.bin, or pass --allow-plain-kernel if you mean it.")

if mode == "fat":
    print(f"{base}: {len(data)} bytes -> FAT /{fat_dest}")
elif mode == "boot":
    print(f"{base}: {len(data)} bytes -> RAM {LOAD_ADDR}, then bootm (card untouched)")
else:
    print(f"{base}: {len(data)} bytes -> {blocks} sectors (0x{blocks:x}) at 0x{SPL_SECTOR:x}")

ser = A.open_serial(timeout=1.0)

# --- be at `=>` before anything is sent ---------------------------------------
if catch:
    if not A.ub_catch_prompt(ser):
        sys.exit("did not reach the `=>` prompt within 30s — nothing written")
    print("at U-Boot prompt (caught)")
elif not A.ub_confirm_prompt(ser):
    sys.exit("not at the `=>` prompt — nothing written. Re-run with --catch to "
             "reboot and catch it here.")

# --- transfer + crc32 in RAM ----------------------------------------------------
want = A.ub_loady(ser, LOAD_ADDR, data, base, baud=baud)

# --- RAM boot -------------------------------------------------------------------
if mode == "boot":
    out = A.ub_send(ser, "fatload mmc 0:1 ${fdt_addr_r} ${fdtfile}", 3.0)
    if "bytes read" not in out.lower():
        sys.exit(f"could not load the DTB from the card:\n{out}")
    A.ub_send(ser, f"setenv bootargs {BOOTARGS}", 0.5)
    ser.reset_input_buffer()
    ser.write(f"bootm {LOAD_ADDR} - ${{fdt_addr_r}}\n".encode())
    ser.flush()
    ser.timeout = 0.05
    text = A.watch_boot(ser, timeout=60)
    sys.stdout.write(text)
    r = A.boot_report(text)
    print()
    if r["login"] and not r["oops"]:
        print("RAM boot reached login. This kernel is gone on the next reboot.")
        sys.exit(0)
    sys.exit(f"RAM boot did not reach login: {r}")

# --- commit to the card ---------------------------------------------------------
print(A.ub_send(ser, "mmc dev 0", 1.5))

if mode == "fat":
    out = A.ub_send(ser, f"fatwrite mmc 0:1 {LOAD_ADDR} {fat_dest} {len(data):x}", 8.0)
    print(out)
    if "bytes written" not in out.lower():
        sys.exit("fatwrite did not report success — do NOT reset; check the console")
    # Read the file back through the filesystem and check exactly its source
    # size: catches a bad media write and a wrong partition or filename alike.
    out = A.ub_send(ser, f"fatload mmc 0:1 {READBACK} {fat_dest}", 8.0)
    print(out)
    if "bytes read" not in out.lower():
        sys.exit("fatload readback failed — do NOT reset; rewrite the file")
    got = A.ub_crc32(ser, READBACK, len(data))
    if got != want:
        sys.exit(f"READBACK MISMATCH for {fat_dest} (wanted {want}, got {got}). "
                 "Do NOT reset; rewrite the file.")
    print(f"/{fat_dest} readback verified ({want})")
    ser.close()
    sys.exit(0)

out = A.ub_send(ser, f"mmc write {LOAD_ADDR} {SPL_SECTOR:x} {blocks:x}", 3.0)
print(out)
if "OK" not in out and "written" not in out.lower():
    sys.exit("mmc write did not report success — do NOT power cycle; check the console")

# The RAM check proves the transfer; this proves the card holds it. A board
# that then fails to boot is the setting's fault, not the flashing's.
A.ub_send(ser, f"mmc read {READBACK} {SPL_SECTOR:x} {blocks:x}", 3.0)
got = A.ub_crc32(ser, READBACK, len(data))
if got != want:
    sys.exit(f"READBACK MISMATCH — the card does not hold the file (wanted {want}, got {got}). "
             "Do NOT power cycle; reflash before resetting.")
print(f"readback from card verified ({want})")
print("Written. Reset the board to run the new U-Boot.")
ser.close()
