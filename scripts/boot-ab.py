#!/usr/bin/env -S uv run --script
# /// script
# dependencies = ["pyserial"]
# ///
"""A/B the SPL Falcon boot against the U-Boot proper path on the same card.

Marker: the USB console node vanishing after `reboot -f` and coming back once
the new kernel's usb-proxy has bound the persistent gadget and macOS has
enumerated it (~10 ms resolution, no adb 1 Hz quantisation). A "uboot" run
writes FLCN into RTC GP1 first so the SPL hands over to U-Boot proper (same
kernel, from the FAT copy); a "falcon" run reboots plainly. Needs the USB
console (DEVELOPMENT.md section 7).

    uv run scripts/boot-ab.py falcon uboot falcon uboot falcon uboot
"""
import os, pathlib, sys, time
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import applib as A
NODE = "/dev/cu.usbmodemUSBPROXY011"
modes = sys.argv[1:] or ["falcon", "uboot"] * 3
for mode in modes:
    ser = A.open_serial(timeout=0.2)
    if not A.lx_login(ser):
        sys.exit("no shell")
    path = A.lx_run(ser, "[ -d /proc/device-tree/psci ] && echo psci || echo nopsci")
    if mode == "uboot":
        A.lx_run(ser, "PATH=$PATH:/usr/sbin:/sbin devmem 0x01f00104 32 0x464c434e")
    ser.write(b"reboot -f\n"); ser.flush()
    t_cmd = time.time()
    try: ser.close()
    except Exception: pass
    while os.path.exists(NODE) and time.time() < t_cmd + 10: time.sleep(0.005)
    t_gone = time.time()
    while not os.path.exists(NODE) and time.time() < t_gone + 60: time.sleep(0.005)
    t_back = time.time()
    print(f"{mode:7} (was {path.strip() if path else '?'}): cmd->gone {t_gone-t_cmd:.2f} s, gone->back {t_back-t_gone:.2f} s", flush=True)
    time.sleep(8)
