#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.9"
# dependencies = ["pyserial"]
# ///
"""appliance.py — build, deploy and verify the usb-proxy appliance from the Mac.

One entry point for the three ways a change reaches the board, each followed
by the same `check` so "did it take?" is never a guess:

    swap      RAM-only usb-proxy binary. For proxy iterations. Gone on reboot.
    boot-ram  RAM-only whole image: the bundled uImage is loaded into RAM over
              the U-Boot console and booted; the card is not written. For
              kernel / initramfs / launcher / config changes. Gone on reboot.
    flash     the final image, card in the board: uImage (and DTB, boot.scr,
              U-Boot only when they differ from the card) over the U-Boot
              console, verified by readback, then reset and check.
              --usb does the same over the OTG port instead of the UART:
              the board is asked to reboot into U-Boot's DFU mode
              (/usr/bin/usb-flash-mode sets an RTC flag) and dfu-util writes
              and reads back the files at USB speed. Needs `brew install
              dfu-util` and a card whose U-Boot/boot.scr already carry the
              DFU support (flash them once over the UART).
    check     what must be true after any of the above: DRAM banner 256 MiB,
              watchdog armed, no oops, login reached, /etc/buildinfo and the
              usb-proxy md5 match the build, the real proxy is running, pstore
              empty, and (--adb) the watch shows up in `adb devices`.
    soak N    reboot N times and grade each boot (scripts/boot-soak.py);
              --cold asks you to power-cycle instead (or runs --power-cmd),
              and a boot that oopsed has its pstore records saved and cleared.

Typical use:

    uv run scripts/appliance.py swap              # devtool workspace -> board
    uv run scripts/appliance.py boot-ram          # deploy dir uImage -> RAM
    uv run scripts/appliance.py flash --adb       # deploy dir -> card, verify
    uv run scripts/appliance.py flash --usb --adb # same, over the OTG port
    uv run scripts/appliance.py check --adb

Build side: the Yocto tree lives in the OrbStack machine at ~/yocto/usbproxy
and is read from the Mac through ~/OrbStack/debian (YOCTO_DIR overrides).
Builds are run with `orb run -m debian`. The serial node is found
automatically when one dongle is present (PI_DEV overrides).

Transfers: a binary swap is ~80 s (printf-octal over the getty at 0.5 KB/s);
a uImage over Y-modem is 136 s at the default 750000 (U-Boot with
CONFIG_SYS_LOADS_BAUD_CHANGE, on the card since 2026-09-09; the script falls
back to 115200, 653 s, if U-Boot does not offer the switch; --baud 0 forces
it). 750000 is the only fast rate to use: it is what both the H3 UART and the
CH340 produce exactly.

The kernel file is always uImage-initramfs-*.bin. The plain uImage-*.bin next
to it is kernel-only and hangs at "Starting kernel" (DEVELOPMENT.md §6).
"""
import argparse
import glob
import gzip
import hashlib
import os
import pathlib
import subprocess
import sys
import time

import applib as A

HERE = pathlib.Path(__file__).resolve().parent
REPO = HERE.parent
YOCTO = pathlib.Path(os.environ.get(
    "YOCTO_DIR", os.path.expanduser("~/OrbStack/debian/home/darrel/yocto/usbproxy")))
DEPLOY = YOCTO / "tmp/deploy/images/orange-pi-zero"
LOGDIR = pathlib.Path(os.path.expanduser("~/.cache/appliance"))
LOAD_ADDR = "0x42000000"  # == ${kernel_addr_r} on this board

# Deploy artifact -> FAT filename on the card.
FAT_FILES = {
    "uImage": "uImage-initramfs-orange-pi-zero.bin",
    "sun8i-h2-plus-orangepi-zero.dtb": "sun8i-h2-plus-orangepi-zero.dtb",
    "boot.scr": "boot.scr",
}
UBOOT_FILE = "u-boot-sunxi-with-spl.bin"
INITRAMFS = "usbproxy-initramfs-orange-pi-zero.cpio.gz"

# U-Boot's DFU gadget (USB_GADGET_VENDOR_NUM/PRODUCT_NUM defaults for sunxi) and
# the alt names usbproxy-boot.cmd puts in dfu_alt_info: the FAT files are alts
# under their own names, "u-boot" is the raw SPL region (sector 0x10, 1 MiB).
DFU_ID = "1f3a:1010"
DFU_UBOOT_ALT = "u-boot"
DFU_UBOOT_RAW_BYTES = 0x800 * 512

OE = ("export LANG=en_US.UTF-8 LC_ALL=en_US.UTF-8; cd ~/yocto/usbproxy && "
      "source layers/poky/oe-init-build-env build >/dev/null && ")


def say(msg):
    print(msg, flush=True)


def fail(msg):
    sys.exit(f"FAIL: {msg}")


def orb(cmd, capture=False):
    """Run a shell command inside the OrbStack build machine."""
    r = subprocess.run(["orb", "run", "-m", "debian", "bash", "-lc", OE + cmd],
                       capture_output=capture, text=True)
    if capture:
        return r.returncode, r.stdout + r.stderr
    return r.returncode, ""


def open_log(tag):
    LOGDIR.mkdir(parents=True, exist_ok=True)
    p = LOGDIR / f"{tag}-{time.strftime('%Y%m%d-%H%M%S')}.log"
    say(f"console transcript: {p}")
    return open(p, "ab")


def md5(data):
    return hashlib.md5(data).hexdigest()


def deploy_path(name):
    p = DEPLOY / name
    if not p.exists():
        fail(f"{p} not found — is the OrbStack machine running and the image built?")
    return p


def build_identity():
    """(buildinfo dict, usb-proxy md5) from the deploy dir's initramfs — what
    the board must report after a flash or boot-ram."""
    d = A.cpio_extract(deploy_path(INITRAMFS), ["usr/bin/usb-proxy", "etc/buildinfo"])
    info = A.parse_buildinfo(d.get("etc/buildinfo", b"").decode(errors="replace"))
    return info, md5(d["usr/bin/usb-proxy"])


def repo_head():
    r = subprocess.run(["git", "-C", str(REPO), "rev-parse", "HEAD"], capture_output=True, text=True)
    return r.stdout.strip()


# --------------------------------------------------------------------------- #
# check
# --------------------------------------------------------------------------- #
class Check:
    def __init__(self):
        self.rows = []
        self.failed = False

    def add(self, ok, name, detail=""):
        self.rows.append((ok, name, detail))
        if ok is False:
            self.failed = True

    def report(self):
        say("")
        for ok, name, detail in self.rows:
            mark = {True: "PASS", False: "FAIL", None: "skip"}[ok]
            say(f"  {mark:4}  {name:34} {detail}")
        say("")
        say("CHECK " + ("FAILED" if self.failed else "PASSED"))
        return not self.failed


def check_boot_text(c, text, from_power_on=True):
    """Grade a boot log. A RAM boot starts at bootm, so the SPL banner and the
    U-Boot watchdog line are not part of it."""
    r = A.boot_report(text)
    if from_power_on:
        c.add(r["dram_ok"], "SPL DRAM banner",
              f"{r['dram_mib']} MiB" if r["dram_mib"] else "no banner seen")
        c.add(r["wdt_started"], "watchdog armed by U-Boot")
    c.add(not r["oops"], "no oops / panic", r.get("oops_at", ""))
    c.add(r["login"], "reached login prompt")
    return r


def check_running(c, ser, expect_md5=None, expect_info=None, adb=False):
    if not A.lx_login(ser):
        c.add(False, "shell over serial", "no shell (at U-Boot? hung?)")
        return
    c.add(True, "shell over serial")

    bi = A.lx_run(ser, "cat /etc/buildinfo 2>/dev/null | grep -E 'DATETIME|meta-usbproxy'")
    info = A.parse_buildinfo(bi or "")
    if expect_info is not None:
        if "DATETIME" not in info:
            c.add(False, "/etc/buildinfo present", "missing (image predates buildinfo)")
        else:
            same = info.get("DATETIME") == expect_info.get("DATETIME")
            c.add(same, "image is the deploy-dir build",
                  f"board {info.get('DATETIME')} / deploy {expect_info.get('DATETIME')}")
            rev = info.get("meta-usbproxy")
            if isinstance(rev, tuple):
                head = repo_head()
                note = f"{rev[1][:9]}{' +modified' if rev[2] else ''}"
                c.add(rev[1] == head or None, "image built from repo HEAD",
                      note + ("" if rev[1] == head else f" (HEAD {head[:9]})"))
    else:
        c.add(None, "image build stamp", info.get("DATETIME", "no buildinfo"))

    out = A.lx_run(ser, "md5sum /usr/bin/usb-proxy")
    got = (out or "").split()[0] if out else None
    if expect_md5:
        c.add(got == expect_md5, "usb-proxy md5 matches", f"{got}")
    else:
        c.add(None, "usb-proxy md5", f"{got}")

    out = A.lx_run(ser, "ps w | grep -c '[/]usr/bin/usb-proxy --device'")
    n = int(out.strip()) if out and out.strip().isdigit() else 0
    c.add(n == 1, "one real usb-proxy running", f"{n} instance(s)")

    out = A.lx_run(ser, "ls /sys/fs/pstore 2>/dev/null | grep -c dmesg-ramoops")
    n = int(out.strip()) if out and out.strip().isdigit() else 0
    c.add(n == 0, "pstore has no crash record", f"{n} dmesg-ramoops record(s)")

    out = A.lx_run(ser, "cat /sys/class/udc/*/state 2>/dev/null; tail -n 1 /var/volatile/log/usb-proxy.log")
    c.add(None, "UDC state / last log line", (out or "").replace("\r\n", " | ")[:100])

    if adb:
        c.add(adb_sees_device(), "watch in `adb devices` on the Mac")


def adb_sees_device(wait=15):
    """True if adb lists a device within `wait` s. On a miss, clears a wedged
    Mac USB port once (scripts/mac-usb-unwedge.py) and looks again."""
    def look():
        end = time.time() + wait
        while time.time() < end:
            r = subprocess.run(["adb", "devices"], capture_output=True, text=True)
            if any(l.split()[-1:] == ["device"] for l in r.stdout.splitlines()[1:]):
                return True
            time.sleep(1)
        return False
    if look():
        return True
    say("adb sees nothing; clearing a possibly wedged Mac USB port once")
    subprocess.run(["uv", "run", str(HERE / "mac-usb-unwedge.py")], cwd=REPO)
    return look()


def cmd_check(args):
    c = Check()
    ser = A.open_serial()
    expect_info, expect_md5 = (None, None)
    if not args.no_expect:
        expect_info, expect_md5 = build_identity()
    if args.expect_md5:
        expect_md5 = args.expect_md5
    if args.reboot:
        log = open_log("check")
        if not A.ub_catch_prompt(ser):
            fail("could not reach the U-Boot prompt to reboot")
        text = A.ub_reset_and_watch(ser, log_file=log)
        check_boot_text(c, text)
        time.sleep(2)
    check_running(c, ser, expect_md5, expect_info, adb=args.adb)
    ser.close()
    sys.exit(0 if c.report() else 1)


# --------------------------------------------------------------------------- #
# swap
# --------------------------------------------------------------------------- #
def swap_source(args):
    """Path of the stripped usb-proxy binary to send."""
    if args.file:
        return pathlib.Path(args.file)
    rc, out = orb("devtool status 2>&1", capture=True)
    in_workspace = "usb-proxy:" in out
    if in_workspace:
        say("devtool workspace holds usb-proxy: building the working tree")
        if not args.no_build:
            rc, _ = orb("devtool build usb-proxy")
            if rc:
                fail("devtool build usb-proxy failed")
        cands = glob.glob(str(YOCTO / "tmp/work/*/usb-proxy/*/packages-split/usb-proxy/usr/bin/usb-proxy"))
        if not cands:
            fail("no packages-split binary after devtool build")
        return pathlib.Path(max(cands, key=os.path.getmtime))
    say("no devtool workspace: sending the pinned build from the deploy-dir initramfs "
        "(use `devtool modify --no-extract usb-proxy /Users/darrel/Downloads/usb-proxy` "
        "to iterate on the source)")
    d = A.cpio_extract(deploy_path(INITRAMFS), ["usr/bin/usb-proxy"])
    p = LOGDIR / "usb-proxy.pinned"
    LOGDIR.mkdir(parents=True, exist_ok=True)
    p.write_bytes(d["usr/bin/usb-proxy"])
    return p


def cmd_swap(args):
    src = swap_source(args)
    data = src.read_bytes()
    want = md5(data)
    if len(data) > 300_000:
        say(f"note: {len(data)} bytes looks unstripped; the upload runs at ~0.5 KB/s")
    LOGDIR.mkdir(parents=True, exist_ok=True)
    gz = LOGDIR / "up.gz"
    gz.write_bytes(gzip.compress(data, 9))
    say(f"{src.name}: {len(data)} bytes, md5 {want}, {gz.stat().st_size} bytes gzipped")

    # Upload with the proven printf-octal uploader (own serial session).
    r = subprocess.run(["uv", "run", str(HERE / "serial-upload.py"), str(gz), "/tmp/up.gz"],
                       cwd=REPO)
    if r.returncode:
        fail("serial-upload.py failed")

    ser = A.open_serial()
    if not A.lx_login(ser):
        fail("no shell after the upload")
    before = A.lx_run(ser, "grep -c 'exec usb-proxy' /var/volatile/log/usb-proxy.log") or "0"
    out = A.lx_run(ser, "gunzip -f /tmp/up.gz && chmod +x /tmp/up && mv /tmp/up /usr/bin/usb-proxy "
                        "&& md5sum /usr/bin/usb-proxy", timeout=15)
    got = (out or "").split()[0] if out else None
    if got != want:
        fail(f"installed md5 {got} != {want}")
    say(f"/usr/bin/usb-proxy installed ({want})")
    # kill -9: SIGTERM takes the graceful path, which can hang on the still-
    # connected host (DEVELOPMENT.md §7). inittab respawns the new binary.
    A.lx_run(ser, "kill -9 $(pidof usb-proxy)")
    time.sleep(4)
    after = A.lx_run(ser, "grep -c 'exec usb-proxy' /var/volatile/log/usb-proxy.log") or "0"
    c = Check()
    c.add(int(after.strip() or 0) > int(before.strip() or 0), "launcher respawned the proxy",
          f"exec lines {before.strip()} -> {after.strip()}")
    check_running(c, ser, expect_md5=want, expect_info=None, adb=args.adb)
    ser.close()
    say("Reminder: this binary lives in RAM only; bake it into the image when done.")
    sys.exit(0 if c.report() else 1)


# --------------------------------------------------------------------------- #
# boot-ram
# --------------------------------------------------------------------------- #
def kernel_file(arg):
    p = pathlib.Path(arg) if arg else deploy_path(FAT_FILES["uImage"])
    if "initramfs" not in p.name:
        fail(f"{p.name}: not the uImage-initramfs build (kernel-only hangs at 'Starting kernel')")
    return p


def cmd_boot_ram(args):
    p = kernel_file(args.uimage)
    data = p.read_bytes()
    expect_info, expect_md5 = build_identity()
    say(f"{p.name}: {len(data)} bytes -> RAM, card untouched")
    log = open_log("boot-ram")
    ser = A.open_serial(timeout=1.0)
    if not A.ub_catch_prompt(ser):
        fail("did not reach the U-Boot prompt")
    say("at U-Boot prompt")
    card_size, card_crc = A.ub_fat_crc(ser, "uImage")
    A.ub_loady(ser, LOAD_ADDR, data, p.name, baud=args.baud or None, progress=say)
    text = A.ub_ram_boot(ser, LOAD_ADDR, log_file=log)
    c = Check()
    check_boot_text(c, text, from_power_on=False)
    time.sleep(2)
    check_running(c, ser, expect_md5, expect_info, adb=args.adb)
    ser.close()
    c.add(None, "card /uImage before boot", f"{card_size} bytes crc {card_crc} (unchanged: RAM boot)")
    say("The card's own kernel comes back on the next reboot.")
    sys.exit(0 if c.report() else 1)


# --------------------------------------------------------------------------- #
# flash
# --------------------------------------------------------------------------- #
def cmd_flash(args):
    if args.usb:
        return cmd_flash_usb(args)
    files = {}
    for fat, dep in FAT_FILES.items():
        p = kernel_file(None) if fat == "uImage" else deploy_path(dep)
        files[fat] = p.read_bytes()
    uboot = deploy_path(UBOOT_FILE).read_bytes()
    expect_info, expect_md5 = build_identity()
    log = open_log("flash")
    ser = A.open_serial(timeout=1.0)
    if not A.ub_catch_prompt(ser):
        fail("did not reach the U-Boot prompt")
    say("at U-Boot prompt")

    # U-Boot first, on its own reset, so a bad SPL shows up while the old
    # kernel is still on the card.
    want = A.crc32_of(uboot)
    on_card = A.ub_spl_crc(ser, len(uboot))
    if args.uboot or (on_card != want and args.auto_uboot):
        say(f"U-Boot differs from the card ({on_card} vs {want}): writing it")
        A.ub_loady(ser, LOAD_ADDR, uboot, UBOOT_FILE, baud=args.baud or None, progress=say)
        A.ub_spl_write(ser, LOAD_ADDR, uboot, want, log=say)
        say("resetting into the new U-Boot")
        if not A.ub_catch_prompt(ser):
            fail("no U-Boot prompt after the U-Boot update — check the console")
        text = A.drain(ser, 0.5)
    elif on_card != want:
        say(f"note: U-Boot on the card ({on_card}) differs from the deploy dir ({want}); "
            "pass --uboot to update it")
    else:
        say("U-Boot unchanged")

    for fat, data in files.items():
        want = A.crc32_of(data)
        size, crc = A.ub_fat_crc(ser, fat)
        if size == len(data) and crc == want and not args.force:
            say(f"/{fat} unchanged ({want})")
            continue
        say(f"/{fat}: card has {size} bytes crc {crc}, sending {len(data)} bytes crc {want}")
        A.ub_loady(ser, LOAD_ADDR, data, fat, baud=args.baud or None, progress=say)
        A.ub_fat_write(ser, LOAD_ADDR, fat, data, want, log=say)

    say("resetting")
    text = A.ub_reset_and_watch(ser, log_file=log)
    c = Check()
    check_boot_text(c, text)
    time.sleep(2)
    check_running(c, ser, expect_md5, expect_info, adb=args.adb)
    ser.close()
    sys.exit(0 if c.report() else 1)


# --------------------------------------------------------------------------- #
# flash --usb (U-Boot DFU over the OTG port)
# --------------------------------------------------------------------------- #
def dfu(*args, timeout=900):
    cmd = ["dfu-util", "-d", DFU_ID] + [str(a) for a in args]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError:
        fail("dfu-util not found: brew install dfu-util")
    return r.returncode, r.stdout + r.stderr


def dfu_present():
    rc, out = dfu("-l", timeout=30)
    return rc == 0 and DFU_ID in out and "Found DFU" in out


def dfu_wait(timeout=60):
    end = time.time() + timeout
    while time.time() < end:
        if dfu_present():
            return True
        time.sleep(1)
    return False


def dfu_upload(alt, size=None):
    """Read an alt back (the FAT file, or `size` raw bytes)."""
    LOGDIR.mkdir(parents=True, exist_ok=True)
    tmp = LOGDIR / f"dfu-upload-{alt}"
    tmp.unlink(missing_ok=True)
    args = ["-a", alt, "-U", str(tmp)]
    if size:
        args += ["-Z", str(size)]
    rc, out = dfu(*args)
    if rc != 0 or not tmp.exists():
        # An absent FAT file reads back as an error; treat it as empty.
        return b""
    return tmp.read_bytes()


def dfu_download(alt, data, name, reset=False):
    """Write an alt. reset=True adds -R: dfu-util sends DFU_DETACH and a USB
    reset after the download, which is what makes U-Boot's dfu loop return and
    do_reset() -- the one clean way out of DFU mode without a console."""
    LOGDIR.mkdir(parents=True, exist_ok=True)
    tmp = LOGDIR / f"dfu-download-{alt}"
    tmp.write_bytes(data)
    t0 = time.time()
    args = ["-a", alt, "-D", str(tmp)] + (["-R"] if reset else [])
    rc, out = dfu(*args)
    if rc != 0 and not (reset and "Resetting USB" in out):
        fail(f"dfu-util download of {name} failed:\n{out[-800:]}")
    say(f"/{name}: {len(data)} bytes in {time.time() - t0:.1f} s")


def request_flash_mode():
    """Ask the running appliance to reboot into U-Boot DFU. Works over the
    UART or the USB console; the USB console node dies with the reboot."""
    ser = A.open_serial()
    node = ser.port
    try:
        if not A.lx_login(ser):
            fail("no shell on the appliance; is it running? (U-Boot prompt: type `reset`)")
        ser.write(b"usb-flash-mode\n")
        ser.flush()
        time.sleep(1.0)
        try:
            out = A.drain(ser, 1.0)
        except Exception:
            out = ""
        if "not found" in out:
            fail("usb-flash-mode is not on this image; flash once over the UART first")
    finally:
        try:
            ser.close()
        except Exception:
            pass
    return node


def wait_for_console(node, timeout=90):
    """After a reset: the UART is always there; a USB console node comes back
    with the idle console (~2 s after usb-proxy starts) or the proxied device."""
    if "usbmodem" not in node:
        return node
    end = time.time() + timeout
    while time.time() < end:
        nodes = A.console_nodes()
        if nodes:
            time.sleep(1.0)
            return nodes[0]
        time.sleep(0.5)
    return None


def cmd_flash_usb(args):
    files = {}
    for fat, dep in FAT_FILES.items():
        p = kernel_file(None) if fat == "uImage" else deploy_path(dep)
        files[fat] = p.read_bytes()
    uboot = deploy_path(UBOOT_FILE).read_bytes()
    expect_info, expect_md5 = build_identity()
    if subprocess.run(["which", "dfu-util"], capture_output=True).returncode != 0:
        fail("dfu-util not found: brew install dfu-util")

    if dfu_present():
        say("board is already in DFU mode")
        node = A.serial_node()
    else:
        node = request_flash_mode()
        say(f"asked the board (via {node}) to reboot into DFU; waiting for {DFU_ID}")
        if not dfu_wait(60):
            fail("no DFU device showed up within 60 s. Card without the DFU-capable "
                 "U-Boot/boot.scr? Flash once over the UART: appliance.py flash --uboot")
    say("DFU device present")

    # U-Boot first, like the serial path. The raw alt reads back the whole
    # 1 MiB region; only the file-length prefix is meaningful.
    want = A.crc32_of(uboot)
    back = dfu_upload(DFU_UBOOT_ALT, size=DFU_UBOOT_RAW_BYTES)[:len(uboot)]
    on_card = A.crc32_of(back) if back else None
    if args.uboot or (on_card != want and args.auto_uboot):
        say(f"U-Boot differs from the card ({on_card} vs {want}): writing it")
        dfu_download(DFU_UBOOT_ALT, uboot, UBOOT_FILE)
        back = dfu_upload(DFU_UBOOT_ALT, size=DFU_UBOOT_RAW_BYTES)[:len(uboot)]
        if A.crc32_of(back) != want:
            fail("U-Boot readback does not match what was written; do NOT power off, "
                 "rerun with --uboot")
        say("U-Boot verified by readback")
    elif on_card != want:
        say(f"note: U-Boot on the card ({on_card}) differs from the deploy dir ({want}); "
            "pass --uboot to update it")
    else:
        say("U-Boot unchanged")

    for fat, data in files.items():
        want = A.crc32_of(data)
        back = dfu_upload(fat)
        crc = A.crc32_of(back) if back else None
        if len(back) == len(data) and crc == want and not args.force:
            say(f"/{fat} unchanged ({want})")
            continue
        say(f"/{fat}: card has {len(back)} bytes crc {crc}, sending {len(data)} bytes crc {want}")
        dfu_download(fat, data, fat)
        back = dfu_upload(fat)
        if len(back) != len(data) or A.crc32_of(back) != want:
            fail(f"/{fat} readback mismatch ({len(back)} bytes, crc "
                 f"{A.crc32_of(back) if back else None}); rerun")
        say(f"/{fat} verified by readback")

    # Leave DFU: a download with -R (detach + USB reset) is what U-Boot's dfu
    # loop recognises as "done, reset". boot.scr is 2 KB and already verified
    # above (or unchanged), so writing it once more is the cheapest vehicle.
    say("leaving DFU: resetting the board")
    dfu_download("boot.scr", files["boot.scr"], "boot.scr (reset vehicle)", reset=True)

    c = Check()
    log = open_log("flash-usb")
    if "usbmodem" in node:
        say("USB console in use: no boot log to grade; waiting for the console to return")
        back_node = wait_for_console(node)
        if not back_node:
            c.add(False, "console back after reset", "no /dev/cu.usbmodem* within 90 s")
            sys.exit(0 if c.report() else 1)
        os.environ["PI_DEV"] = back_node
        ser = A.open_serial()
    else:
        ser = A.open_serial(timeout=0.05)
        text = A.watch_boot(ser, timeout=60, log=log)
        check_boot_text(c, text)
        time.sleep(2)
    check_running(c, ser, expect_md5, expect_info, adb=args.adb)
    ser.close()
    sys.exit(0 if c.report() else 1)


# --------------------------------------------------------------------------- #
def cmd_soak(args):
    LOGDIR.mkdir(parents=True, exist_ok=True)
    tag = "soak-cold" if args.cold else "soak"
    logp = LOGDIR / f"{tag}-{time.strftime('%Y%m%d-%H%M%S')}.log"
    cmd = ["uv", "run", str(HERE / "boot-soak.py"), str(args.n), str(logp)]
    if args.cold:
        cmd.append("--cold")
    if args.power_cmd:
        cmd += ["--power-cmd", args.power_cmd]
    say(f"console transcript: {logp} (oops records land next to it)")
    r = subprocess.run(cmd, cwd=REPO)
    sys.exit(r.returncode)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("swap", help="RAM-only usb-proxy binary")
    p.add_argument("file", nargs="?", help="stripped ARM binary (default: devtool workspace build, else the pinned build)")
    p.add_argument("--no-build", action="store_true", help="skip `devtool build`")
    p.add_argument("--adb", action="store_true")
    p.set_defaults(fn=cmd_swap)

    p = sub.add_parser("boot-ram", help="RAM-only whole image, card untouched")
    p.add_argument("uimage", nargs="?", help="uImage-initramfs-*.bin (default: deploy dir)")
    p.add_argument("--baud", type=int, default=750000, help="Y-modem rate (default 750000; 0 = stay at 115200)")
    p.add_argument("--adb", action="store_true")
    p.set_defaults(fn=cmd_boot_ram)

    p = sub.add_parser("flash", help="final image onto the card, then reset and check")
    p.add_argument("--usb", action="store_true",
                   help="over the OTG port via U-Boot DFU (dfu-util) instead of the UART")
    p.add_argument("--uboot", action="store_true", help="also write U-Boot/SPL")
    p.add_argument("--auto-uboot", action="store_true", help="write U-Boot when it differs from the card")
    p.add_argument("--force", action="store_true", help="rewrite FAT files even when unchanged")
    p.add_argument("--baud", type=int, default=750000, help="Y-modem rate (default 750000; 0 = stay at 115200)")
    p.add_argument("--adb", action="store_true")
    p.set_defaults(fn=cmd_flash)

    p = sub.add_parser("check", help="verify what the board is running")
    p.add_argument("--reboot", action="store_true", help="reboot first and grade the boot log")
    p.add_argument("--adb", action="store_true")
    p.add_argument("--expect-md5", help="usb-proxy md5 to expect (after a swap)")
    p.add_argument("--no-expect", action="store_true", help="do not compare against the deploy dir")
    p.set_defaults(fn=cmd_check)

    p = sub.add_parser("soak", help="reboot (or power-cycle) N times and grade each boot")
    p.add_argument("n", type=int, nargs="?", default=5)
    p.add_argument("--cold", action="store_true", help="power-cycle instead of reboot (prompts you)")
    p.add_argument("--power-cmd", help="command that cycles the supply, for --cold")
    p.set_defaults(fn=cmd_soak)

    args = ap.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
