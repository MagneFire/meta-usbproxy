"""applib.py — the serial-console plumbing shared by the scripts in this directory.

Not a script. The uv scripts next to it (`pi-serial.py`, `uboot-flash.py`,
`appliance.py`, ...) import it from their own directory; it needs only pyserial,
which each of them declares inline.

Three layers, all over the one USB-UART dongle at 115200 8N1:

  serial_node()            which /dev/tty.usbserial-* to open (PI_DEV wins)
  lx_*                     the BusyBox getty/ash side: log in as root, run a command
  ub_*                     the U-Boot side: reach `=>`, run a command, Y-modem a file
  read_until / boot_report watch a boot and grade it (DRAM banner, WDT, oops, login)

Everything that used to live as a copy in each script is here once.
"""
import glob
import os
import random
import re
import sys
import time
import zlib

import serial

BAUD = 115200

# Y-modem control bytes.
SOH, STX, EOT, ACK, NAK, CAN, CRC = 0x01, 0x02, 0x04, 0x06, 0x15, 0x18, 0x43


# --------------------------------------------------------------------------- #
# Port
# --------------------------------------------------------------------------- #
def serial_node():
    """The dongle's device node. PI_DEV overrides; otherwise the single
    /dev/tty.usbserial-* present. The node name changes between dongles and
    reconnects (-10, -110, -11410 have all been seen), which is why there is no
    hard-coded default any more."""
    dev = os.environ.get("PI_DEV")
    if dev:
        return dev
    nodes = sorted(glob.glob("/dev/tty.usbserial-*"))
    if len(nodes) == 1:
        return nodes[0]
    if not nodes:
        sys.exit("no /dev/tty.usbserial-* present: plug in the USB-UART dongle, "
                 "or set PI_DEV")
    sys.exit("several USB-UART nodes present, set PI_DEV to one of: " + " ".join(nodes))


def open_serial(timeout=0.05):
    return serial.Serial(serial_node(), BAUD, timeout=timeout)


def drain(ser, seconds):
    """Read whatever arrives for `seconds`, as text."""
    buf = b""
    end = time.time() + seconds
    while time.time() < end:
        d = ser.read(ser.in_waiting or 1)
        if d:
            buf += d
    return buf.decode(errors="replace")


def read_until(ser, patterns, timeout, log=None):
    """Read until any of `patterns` (bytes) shows up in the tail, or timeout.
    Returns (text, matched_pattern_or_None). `log` gets the raw bytes."""
    buf = b""
    end = time.time() + timeout
    while time.time() < end:
        d = ser.read(ser.in_waiting or 1)
        if not d:
            continue
        buf += d
        if log:
            log.write(d)
            log.flush()
        tail = buf[-600:]
        for p in patterns:
            if p in tail:
                return buf.decode(errors="replace"), p
    return buf.decode(errors="replace"), None


# --------------------------------------------------------------------------- #
# Linux side (BusyBox getty + ash)
# --------------------------------------------------------------------------- #
def lx_login(ser):
    """Idempotent: works from a login prompt, a password prompt or a shell.
    Returns False if no shell prompt could be reached (probably at U-Boot, or
    the board is hung)."""
    ser.reset_input_buffer()
    ser.write(b"\n")
    ser.flush()
    out = drain(ser, 1.0)
    tail = out[-300:]
    if "login:" in tail or "incorrect" in tail:
        ser.write(b"root\n")
        ser.flush()
        out = drain(ser, 1.5)
        if "assword" in out[-120:]:
            ser.write(b"\n")
            ser.flush()
            drain(ser, 1.5)
    # Clear any partial line, then prove we have a shell.
    ser.write(b"\x15\n")
    ser.flush()
    drain(ser, 0.3)
    return lx_run(ser, "true", timeout=3.0) is not None


def lx_run(ser, cmd, timeout=8.0):
    """Run `cmd` in the appliance shell and return its output (without the
    echoed command line), or None on timeout.

    The command is wrapped between two sentinels whose *echo* differs from
    their *output* (`$((..))` arithmetic), so the echoed command line, which
    the terminal wraps at 80 columns, can never be mistaken for the result.
    Keep `cmd` well under 900 chars: ash line editing silently truncates at
    1024."""
    if len(cmd) > 900:
        raise ValueError("command too long for the ash line editor")
    n = random.randint(1000, 9999)
    start = f"S{n}"
    endm = f"E{n}"
    wrapped = f"echo S$(({n - 1}+1)); {cmd}; echo E$(({n - 1}+1))\n"
    ser.reset_input_buffer()
    ser.write(wrapped.encode())
    ser.flush()
    text, hit = read_until(ser, [endm.encode() + b"\r"], timeout)
    if hit is None:
        # The echo of the sentinel command carries "E$((...))" not "E1234", so
        # a hit means the command really finished; a miss means it did not.
        return None
    i = text.find(start + "\r")
    j = text.rfind(endm)
    if i < 0 or j < 0:
        return None
    return text[i + len(start):j].strip("\r\n")


# --------------------------------------------------------------------------- #
# U-Boot side
# --------------------------------------------------------------------------- #
def ub_send(ser, cmd, wait=1.0):
    """Type one command at `=>` and return what came back within `wait`."""
    ser.reset_input_buffer()
    ser.write((cmd + "\n").encode())
    ser.flush()
    return drain(ser, wait)


def ub_confirm_prompt(ser, tries=3):
    """True once a bare CR echoes the U-Boot `=>` prompt back."""
    saved = ser.timeout
    ser.timeout = 0.05
    try:
        for _ in range(tries):
            ser.reset_input_buffer()
            ser.write(b"\r\n")
            ser.flush()
            if "=>" in drain(ser, 0.4):
                return True
        return False
    finally:
        ser.timeout = saved


def ub_catch_prompt(ser, timeout=30):
    """Reboot the board from wherever the console is and hammer a key until
    autoboot (bootdelay=0, still interruptible) drops to `=>`.

    From a U-Boot prompt: `reset`. From Linux (login prompt or shell):
    `reboot -f`. A hung kernel answers neither; that needs a power-cycle."""
    ser.reset_input_buffer()
    ser.write(b"\n")
    ser.flush()
    if "=>" in drain(ser, 1.0)[-300:]:
        ser.write(b"reset\n")
    else:
        # Get a verified shell first. Typing `reboot -f` a fixed delay after
        # `root` at a login prompt is a race: login flushes the tty while it
        # starts the shell and the command is lost (seen 2026-09-09).
        lx_login(ser)
        ser.write(b"reboot -f\n")
    ser.flush()
    # The key spam must be continuous: the bootdelay=0 autoboot only checks
    # for a pending key once, and the board goes quiet for stretches (DRAM
    # init, SPL -> U-Boot). A long read timeout here turns the spam into one
    # key per second and the window is missed.
    saved = ser.timeout
    ser.timeout = 0.05
    ser.baudrate = BAUD  # a reboot always comes back at the console default
    buf = b""
    end = time.time() + timeout
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
    ser.timeout = saved
    return got


def ub_crc32(ser, addr, size):
    """crc32 of `size` bytes at `addr` as U-Boot reports it (8 hex chars), or
    None if the reply could not be parsed."""
    out = ub_send(ser, f"crc32 {addr} {size:x}", 3.0)
    m = re.search(r"==>\s*([0-9a-fA-F]{8})", out)
    return m.group(1).lower() if m else None


def crc32_of(data):
    return f"{zlib.crc32(data) & 0xFFFFFFFF:08x}"


def _crc16(buf):
    crc = 0
    for b in buf:
        crc ^= b << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) & 0xFFFF if crc & 0x8000 else (crc << 1) & 0xFFFF
    return crc


def _wait_byte(ser, byte, timeout, label):
    end = time.time() + timeout
    while time.time() < end:
        d = ser.read(1)
        if d and d[0] == byte:
            return True
        if d and d[0] == CAN:
            sys.exit(f"receiver cancelled during {label}")
    return False


def _send_block(ser, seq, payload):
    head = STX if len(payload) > 128 else SOH
    frame = bytes([head, seq & 0xFF, (~seq) & 0xFF]) + payload
    frame += _crc16(payload).to_bytes(2, "big")
    for _ in range(10):
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


# Rates the H3 UART produces exactly (24 MHz / 16 / N) that the CH340 dongle
# also produces exactly (12 MHz based). Asking U-Boot for anything else is a
# trap: it rounds the divisor and prints the rate it was asked for, so
# "921600" is really 750000 on the wire and the Mac side never syncs.
FAST_BAUDS = (1500000, 750000, 500000, 375000)


def ub_loady(ser, addr, data, name, baud=None, progress=print):
    """`loady` `data` to `addr` and verify it landed (crc32 in RAM).

    With `baud`, uses U-Boot's `loady <addr> <baud>` form: U-Boot switches its
    console to that rate for the transfer. That form only exists when U-Boot
    was built with CONFIG_SYS_LOADS_BAUD_CHANGE (see
    recipes-bsp/u-boot/files/usbproxy-uboot.cfg); without it U-Boot ignores
    the extra argument and stays at 115200, which this detects and reports.
    After the transfer U-Boot is supposed to switch back and wait for ESC at
    115200; on this board's UART the console has been seen to stay at the
    fast rate instead, so this probes both and leaves `ser.baudrate` at
    whichever answers the prompt. A later `reset` or `bootm` puts the
    console back to 115200 (ub_reset_and_watch / ub_ram_boot handle that).

    Must be called at the `=>` prompt. Returns the crc32 (hex) of `data`."""
    if baud and baud not in FAST_BAUDS:
        sys.exit(f"--baud {baud}: use one of {FAST_BAUDS} (rates both the H3 UART "
                 "and the CH340 produce exactly; 750000 is the tested one)")
    saved_timeout = ser.timeout
    ser.timeout = 1.0
    ser.reset_input_buffer()
    cmd = f"loady {addr}" + (f" {baud}" if baud else "")
    ser.write((cmd + "\n").encode())
    ser.flush()

    switched = False
    if baud:
        # "## Switch baudrate to N bps and press ENTER ..."
        text, hit = read_until(ser, [b"press ENTER", b"\x43"], 5.0)
        if hit == b"press ENTER":
            ser.baudrate = baud
            time.sleep(0.05)
            ser.reset_input_buffer()
            ser.write(b"\r")
            ser.flush()
            switched = True
        else:
            progress("U-Boot did not offer a baud switch (no CONFIG_SYS_LOADS_BAUD_CHANGE?); "
                     "staying at 115200")
            if hit is None and not _wait_byte(ser, CRC, 15, "loady handshake"):
                sys.exit("U-Boot never asked for a Y-modem transfer — are you at `=>`?")
    if not switched and not baud:
        if not _wait_byte(ser, CRC, 15, "loady handshake"):
            sys.exit("U-Boot never asked for a Y-modem transfer — are you at `=>`?")
    elif switched:
        if not _wait_byte(ser, CRC, 15, "loady handshake"):
            sys.exit(f"no Y-modem handshake at {baud} baud — the dongle or U-Boot "
                     "did not follow the switch; retry without --baud")

    hdr = name.encode() + b"\0" + str(len(data)).encode() + b"\0"
    _send_block(ser, 0, hdr.ljust(128, b"\0"))
    if not _wait_byte(ser, CRC, 15, "header ack"):
        sys.exit("receiver did not request data after the header block")

    t0 = time.time()
    seq = 1
    for off in range(0, len(data), 1024):
        chunk = data[off:off + 1024]
        _send_block(ser, seq, chunk.ljust(1024, b"\x1a"))
        seq += 1
        if seq % 64 == 0 or off + 1024 >= len(data):
            done = min(off + 1024, len(data))
            rate = done / (time.time() - t0)
            progress(f"  {done}/{len(data)} bytes ({rate / 1024:.1f} KB/s)")
    # End of file. U-Boot's xyzModem does not always play the strict
    # NAK-then-ACK dance back (it has been seen to answer CAN); the crc32
    # below is the real check, so just drain here.
    for _ in range(2):
        ser.write(bytes([EOT]))
        ser.flush()
        time.sleep(0.3)
        ser.read(ser.in_waiting or 1)
    elapsed = time.time() - t0

    if switched:
        # "## Switch baudrate to 115200 bps and press ESC ..." — then ESC at
        # both rates, and keep whichever one the prompt answers at.
        read_until(ser, [b"press ESC"], 5.0)
        time.sleep(0.2)
        ser.write(b"\x1b")
        ser.flush()
        time.sleep(0.3)
        ser.read(ser.in_waiting or 1)
        ser.timeout = 0.2
        if not ub_confirm_prompt(ser, tries=2):
            ser.baudrate = BAUD
            time.sleep(0.05)
            ser.write(b"\x1b")
            ser.flush()
            time.sleep(0.3)
            ser.read(ser.in_waiting or 1)
            if not ub_confirm_prompt(ser, tries=2):
                sys.exit("lost the U-Boot prompt after the fast transfer (neither "
                         f"{baud} nor {BAUD} answers)")
        progress(f"U-Boot console now at {ser.baudrate}")
    time.sleep(1.2)
    ser.read(ser.in_waiting or 1)
    ser.timeout = saved_timeout

    want = crc32_of(data)
    got = ub_crc32(ser, addr, len(data))
    if got != want:
        sys.exit(f"CRC32 mismatch — RAM does not hold the file (wanted {want}, got {got})")
    progress(f"crc32 {want} verified in RAM ({len(data) / 1024 / elapsed:.1f} KB/s over {elapsed:.0f} s)")
    return want


# --------------------------------------------------------------------------- #
# Watching a boot
# --------------------------------------------------------------------------- #
BOOT_STOPS = [b" login:", b"Rebooting in", b"=>"]


def watch_boot(ser, timeout=60, log=None):
    """Read the console until a login prompt, a panic reboot notice or a
    U-Boot prompt. Returns the text."""
    text, _ = read_until(ser, BOOT_STOPS, timeout, log=log)
    return text


def boot_report(text):
    """Grade a boot log. Returns a dict of the things that matter here."""
    m = re.search(r"DRAM:\s*(\d+)\s*MiB", text)
    dram = m.group(1) if m else None
    r = {
        "dram_mib": dram,
        "dram_ok": dram == "256",
        "wdt_started": bool(re.search(r"WDT:\s+Started", text)),
        "oops": bool(re.search(r"Kernel panic|Oops|Rebooting in", text)),
        "login": " login:" in text,
    }
    m2 = re.search(r"PC is at ([^\n]+)", text)
    m3 = re.search(r"Comm: (\S+)", text)
    if r["oops"]:
        r["oops_at"] = f"{m3.group(1) if m3 else '?'} @ {m2.group(1).strip() if m2 else '?'}"
    return r


# --------------------------------------------------------------------------- #
# U-Boot: the three things done with a file once it is in RAM
# --------------------------------------------------------------------------- #
READBACK = "0x43000000"
SPL_SECTOR = 0x10      # sunxi SPL offset (8 KiB); wic: `part u-boot --align 8`
SECTOR = 512

# The appliance boot.scr's bootargs (recipes-bsp/u-boot/files/boot.cmd). Kept
# identical so a RAM boot runs the card's cmdline (the kernel is built with
# CMDLINE_EXTEND, so these matter).
BOOTARGS = "console=${console} maxcpus=2 panic=10 ${extra}"


def ub_fat_crc(ser, name):
    """(size, crc32) of FAT file `name` on the card as U-Boot sees it, or
    (None, None) if it is not there."""
    out = ub_send(ser, f"fatsize mmc 0:1 {name}", 2.0)
    if "unable" in out.lower() or "not found" in out.lower():
        return None, None
    out = ub_send(ser, "printenv filesize", 1.0)
    m = re.search(r"filesize=([0-9a-fA-F]+)", out)
    if not m:
        return None, None
    size = int(m.group(1), 16)
    out = ub_send(ser, f"fatload mmc 0:1 {READBACK} {name}", 8.0)
    if "bytes read" not in out.lower():
        return None, None
    return size, ub_crc32(ser, READBACK, size)


def ub_spl_crc(ser, size):
    """crc32 of the first `size` bytes at the SPL sector on the card."""
    blocks = (size + SECTOR - 1) // SECTOR
    ub_send(ser, f"mmc dev 0", 1.5)
    ub_send(ser, f"mmc read {READBACK} {SPL_SECTOR:x} {blocks:x}", 3.0)
    return ub_crc32(ser, READBACK, size)


def ub_fat_write(ser, addr, name, data, want, log=print):
    """fatwrite `data` (already at `addr`, crc `want`) as /`name`, then read it
    back through the filesystem and crc it again. Exits on any doubt: the
    card must never be left in an unknown state."""
    ub_send(ser, "mmc dev 0", 1.5)
    out = ub_send(ser, f"fatwrite mmc 0:1 {addr} {name} {len(data):x}", 8.0)
    if "bytes written" not in out.lower():
        sys.exit(f"fatwrite did not report success — do NOT reset; check the console:\n{out}")
    out = ub_send(ser, f"fatload mmc 0:1 {READBACK} {name}", 8.0)
    if "bytes read" not in out.lower():
        sys.exit("fatload readback failed — do NOT reset; rewrite the file")
    got = ub_crc32(ser, READBACK, len(data))
    if got != want:
        sys.exit(f"READBACK MISMATCH for /{name} (wanted {want}, got {got}). "
                 "Do NOT reset; rewrite the file.")
    log(f"/{name} written and read back ({want})")


def ub_spl_write(ser, addr, data, want, log=print):
    """Write U-Boot (SPL + proper) at sector 0x10 and read it back. The one
    write that can brick the card's boot path, hence the double check."""
    blocks = (len(data) + SECTOR - 1) // SECTOR
    ub_send(ser, "mmc dev 0", 1.5)
    out = ub_send(ser, f"mmc write {addr} {SPL_SECTOR:x} {blocks:x}", 3.0)
    if "OK" not in out and "written" not in out.lower():
        sys.exit(f"mmc write did not report success — do NOT power cycle; check the console:\n{out}")
    ub_send(ser, f"mmc read {READBACK} {SPL_SECTOR:x} {blocks:x}", 3.0)
    got = ub_crc32(ser, READBACK, len(data))
    if got != want:
        sys.exit(f"READBACK MISMATCH — the card does not hold U-Boot (wanted {want}, got {got}). "
                 "Do NOT power cycle; reflash before resetting.")
    log(f"U-Boot written at sector 0x{SPL_SECTOR:x} and read back ({want})")


def ub_ram_boot(ser, addr, timeout=60, log_file=None):
    """bootm the kernel already at `addr` with the card's DTB and the card's
    bootargs. Returns the boot console text."""
    out = ub_send(ser, "fatload mmc 0:1 ${fdt_addr_r} ${fdtfile}", 3.0)
    if "bytes read" not in out.lower():
        sys.exit(f"could not load the DTB from the card:\n{out}")
    ub_send(ser, f"setenv bootargs {BOOTARGS}", 0.5)
    ser.reset_input_buffer()
    ser.write(f"bootm {addr} - ${{fdt_addr_r}}\n".encode())
    ser.flush()
    saved = ser.timeout
    ser.timeout = 0.05
    time.sleep(0.3)
    ser.baudrate = BAUD  # the kernel's console is ttyS0,115200 whatever U-Boot ran at
    text = watch_boot(ser, timeout=timeout, log=log_file)
    ser.timeout = saved
    return text


def ub_reset_and_watch(ser, timeout=60, log_file=None):
    """`reset` at the prompt and follow the boot to login."""
    ser.reset_input_buffer()
    ser.write(b"reset\n")
    ser.flush()
    saved = ser.timeout
    ser.timeout = 0.05
    time.sleep(0.3)
    ser.baudrate = BAUD  # a reset comes back at the console default
    text = watch_boot(ser, timeout=timeout, log=log_file)
    ser.timeout = saved
    return text


# --------------------------------------------------------------------------- #
# Build artifacts (read from the Mac through the OrbStack mount)
# --------------------------------------------------------------------------- #
def cpio_extract(gz_path, wanted):
    """Return {name: bytes} for the `wanted` entries of a gzipped newc cpio
    (the initramfs). No external tools: macOS cpio cannot stream one entry."""
    import gzip
    data = gzip.open(gz_path).read()
    out = {}
    pos = 0
    wanted = set(wanted)
    while pos + 110 <= len(data):
        if data[pos:pos + 6] != b"070701":
            raise ValueError(f"not a newc cpio at offset {pos}")
        f = [int(data[pos + 6 + i * 8:pos + 14 + i * 8], 16) for i in range(13)]
        filesize, namesize = f[6], f[11]
        name = data[pos + 110:pos + 110 + namesize - 1].decode()
        body = pos + 110 + namesize
        body += (-body) % 4
        if name == "TRAILER!!!":
            break
        if name.lstrip("./") in wanted:
            out[name.lstrip("./")] = data[body:body + filesize]
            if len(out) == len(wanted):
                break
        pos = body + filesize
        pos += (-pos) % 4
    return out


def parse_buildinfo(text):
    """{'DATETIME': ..., 'meta-usbproxy': ('main', 'ca597d1...', modified?)}"""
    info = {}
    for line in text.splitlines():
        m = re.match(r"^(\S+)\s*=\s*(.*)$", line.strip())
        if not m:
            continue
        k, v = m.group(1), m.group(2).strip()
        mm = re.match(r"^([^:]+):([0-9a-f]{7,40})(.*)$", v)
        if mm:
            info[k] = (mm.group(1), mm.group(2), bool(mm.group(3).strip()))
        else:
            info[k] = v
    return info
