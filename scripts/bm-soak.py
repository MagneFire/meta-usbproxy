#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = []
# ///
"""bm-soak.py — M4b soak driver for the bare-metal firmware (BARE-METAL.md §8).

Runs unattended against the dev rig (stock Zero with usbproxy-bm as the
Falcon payload, Hoki watch cradled, one adb server on the Mac) and appends a
line per step to ~/.cache/appliance/bm-soak-<stamp>.log plus a summary.

    uv run scripts/bm-soak.py reboots 100          # console reboot -> bm check --adb
    uv run scripts/bm-soak.py concurrent 10        # push + pull at once, shell loop, md5
    uv run scripts/bm-soak.py fastboot 10 [--boot-image IMG]
                                                   # adb reboot bootloader, getvar, [fastboot boot IMG], back to adb
    uv run scripts/bm-soak.py idle 30              # minutes idle, port watched, then rebind check
    uv run scripts/bm-soak.py all                  # reboots 100, concurrent 10, fastboot 10, idle 30

The firmware console is reached through appliance.py (`bm cmd`, `bm check`):
`cmd` over the gadget's CDC node, `check` over the UART dongle so it does not
race the CDC re-enumeration after a reboot.
"""
import argparse
import hashlib
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
UART = os.environ.get("BM_UART", "/dev/tty.usbserial-110")
CDC = os.environ.get("BM_CDC", "/dev/cu.usbmodemUSBPROXYBM1")
OUT = Path.home() / ".cache" / "appliance"
REMOTE_DIR = "/tmp/cb"
LOG = None


def log(msg):
    line = f"{time.strftime('%H:%M:%S')} {msg}"
    print(line, flush=True)
    with open(LOG, "a") as f:
        f.write(line + "\n")


def run(cmd, timeout, env=None):
    """(rc, combined output); rc = -1 on timeout (process killed)."""
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                           env={**os.environ, **(env or {})})
        return r.returncode, (r.stdout + r.stderr)
    except subprocess.TimeoutExpired as e:
        return -1, ((e.stdout or b"").decode(errors="replace") + (e.stderr or b"").decode(errors="replace"))


def appliance(*args, dev, timeout=60):
    return run(["uv", "run", str(HERE / "appliance.py"), *args], timeout, env={"PI_DEV": dev})


def bm_cmd(text, timeout=30):
    return appliance("bm", "cmd", text, dev=CDC, timeout=timeout)


def bm_check_adb(timeout=90):
    rc, out = appliance("bm", "check", "--adb", dev=UART, timeout=timeout)
    fails = [l.strip() for l in out.splitlines() if l.strip().startswith("FAIL")]
    return rc == 0 and not fails, fails or ([l for l in out.splitlines() if "CHECK" in l] or [out[-200:]])


def status_fields(*keys):
    rc, out = bm_cmd("status")
    d = {}
    for l in out.splitlines():
        if "=" in l:
            k, v = l.split("=", 1)
            if k.strip() in keys:
                d[k.strip()] = v.strip()
    return d


def adb(*args, timeout=60):
    return run(["adb", *args], timeout)


def adb_shell_ok(timeout=8):
    rc, out = adb("shell", "echo", "ok", timeout=timeout)
    return rc == 0 and out.strip() == "ok"


def wait_adb(limit_s):
    t0 = time.time()
    while time.time() - t0 < limit_s:
        if adb_shell_ok():
            return time.time() - t0
        time.sleep(1.5)
    return None


def wait_node(path, limit_s):
    t0 = time.time()
    while time.time() - t0 < limit_s:
        if os.path.exists(path):
            return True
        time.sleep(0.5)
    return False


# ---------------------------------------------------------------- reboots
def soak_reboots(n):
    ok = 0
    for i in range(1, n + 1):
        bm_cmd("reboot", timeout=20)
        time.sleep(6)
        node = wait_node(CDC, 25)
        t_adb = wait_adb(60) if node else None
        good, detail = bm_check_adb()
        ok += good
        log(f"reboot {i}/{n}: cdc={'up' if node else 'MISSING'} adb={'%.1fs' % t_adb if t_adb is not None else 'NOT BACK'} "
            f"check={'PASS' if good else 'FAIL ' + '; '.join(detail)}")
        if not good:
            rc, out = appliance("bm", "log", "2500", dev=UART, timeout=60)
            log("   log tail: " + " | ".join(l for l in out.splitlines()[-8:]))
    log(f"reboots: {ok}/{n} passed")
    return ok == n


# ---------------------------------------------------------------- concurrent
class ShellLoop:
    def __init__(self):
        self.stop = threading.Event()
        self.count = 0
        self.fail = 0
        self.t = threading.Thread(target=self.run, daemon=True)

    def run(self):
        while not self.stop.is_set():
            rc, out = adb("shell", "true", timeout=10)
            self.count += 1
            if rc != 0:
                self.fail += 1
            time.sleep(0.2)

    def __enter__(self):
        self.t.start()
        return self

    def __exit__(self, *a):
        self.stop.set()
        self.t.join(timeout=15)


def soak_concurrent(n, size_mb):
    OUT.mkdir(parents=True, exist_ok=True)
    a = OUT / f"soak-a-{size_mb}.bin"
    b = OUT / f"soak-b-{size_mb}.bin"
    for f in (a, b):
        if not f.exists() or f.stat().st_size != size_mb << 20:
            f.write_bytes(os.urandom(size_mb << 20))
    md5a = hashlib.md5(a.read_bytes()).hexdigest()
    md5b = hashlib.md5(b.read_bytes()).hexdigest()
    adb("shell", f"mkdir -p {REMOTE_DIR}", timeout=20)
    # right after a reboot phase the Mac's adb can still be settling
    # ("65544-byte write failed: No buffer space available" seen once): retry
    for attempt in range(4):
        wait_adb(30)
        rc, out = adb("push", str(b), f"{REMOTE_DIR}/b.bin", timeout=300)
        if rc == 0:
            break
        log(f"concurrent: seed push attempt {attempt + 1} failed: {out.strip()[-120:]}")
        time.sleep(5)
    if rc != 0:
        return False
    ok = 0
    with ShellLoop() as loop:
        for i in range(1, n + 1):
            back = OUT / "soak-pulled.bin"
            if back.exists():
                back.unlink()
            res = {}

            def do_push():
                res["push"] = adb("push", str(a), f"{REMOTE_DIR}/a.bin", timeout=300)

            def do_pull():
                res["pull"] = adb("pull", f"{REMOTE_DIR}/b.bin", str(back), timeout=300)

            t0 = time.time()
            th = [threading.Thread(target=do_push), threading.Thread(target=do_pull)]
            for t in th:
                t.start()
            for t in th:
                t.join()
            dt = time.time() - t0
            rc_a, out_a = adb("shell", f"md5sum {REMOTE_DIR}/a.bin", timeout=60)
            rmd5 = out_a.split()[0] if rc_a == 0 and out_a.split() else "?"
            pmd5 = hashlib.md5(back.read_bytes()).hexdigest() if back.exists() else "?"
            good = res["push"][0] == 0 and res["pull"][0] == 0 and rmd5 == md5a and pmd5 == md5b
            ok += good
            st = status_fields("fault", "host_link")
            md5s = f"pushed:{'ok' if rmd5 == md5a else 'BAD'} pulled:{'ok' if pmd5 == md5b else ('missing' if pmd5 == '?' else 'BAD')}"
            log(f"concurrent {i}/{n}: {dt:.1f}s push_rc={res['push'][0]} pull_rc={res['pull'][0]} "
                f"md5 {md5s} shell_loop={loop.count}/{loop.fail}fail fault={st.get('fault')} link={st.get('host_link')}")
            if not good:
                log("   push: " + res["push"][1].strip()[-150:].replace("\n", " | "))
                log("   pull: " + res["pull"][1].strip()[-150:].replace("\n", " | "))
                if not wait_adb(30):
                    log("   adb did not come back; stopping")
                    break
    log(f"concurrent: {ok}/{n} passed (shell loop {loop.count} calls, {loop.fail} failed)")
    return ok == n


# ---------------------------------------------------------------- fastboot
def wait_fastboot(limit_s):
    t0 = time.time()
    while time.time() - t0 < limit_s:
        rc, out = run(["fastboot", "devices"], 15)
        if "fastboot" in out:
            return time.time() - t0
        time.sleep(2)
    return None


def soak_fastboot(n, boot_image):
    ok = 0
    for i in range(1, n + 1):
        rc, out = adb("reboot", "bootloader", timeout=20)
        t_fb = wait_fastboot(90)
        if t_fb is None:
            log(f"fastboot {i}/{n}: no fastboot device within 90 s (adb reboot rc={rc})")
            wait_adb(150)
            continue
        rc_g, out_g = run(["fastboot", "getvar", "product"], 30)
        product = next((l for l in out_g.splitlines() if l.startswith("product:")), out_g.strip()[-60:])
        rc_all, out_all = run(["fastboot", "getvar", "all"], 40)
        nvars = out_all.count("(bootloader)")
        boot_note = ""
        if boot_image:
            rc_b, out_b = run(["fastboot", "boot", boot_image], 120)
            t_badb = wait_adb(150)
            boot_note = f" boot_image rc={rc_b} adb_after_boot={'%.0fs' % t_badb if t_badb else 'NONE'}"
            adb("reboot", timeout=20)
        else:
            rc_r, out_r = run(["fastboot", "reboot"], 30)
            boot_note = f" fastboot_reboot rc={rc_r}"
        t_adb = wait_adb(180)
        good = rc_g == 0 and "product:" in product and nvars > 5 and t_adb is not None
        ok += good
        st = status_fields("fault")
        log(f"fastboot {i}/{n}: fastboot in {t_fb:.0f}s, {product}, getvar all {nvars} vars,{boot_note}, "
            f"adb back {'%.0fs' % t_adb if t_adb else 'NOT BACK'} fault={st.get('fault')} -> {'PASS' if good else 'FAIL'}")
        if t_adb is None:
            log("   adb did not return; stopping")
            break
    log(f"fastboot: {ok}/{n} passed")
    return ok == n


# ---------------------------------------------------------------- idle
def soak_idle(minutes):
    rc, out = adb("devices", timeout=15)
    log(f"idle: start, adb: {' '.join(out.split()[4:])}")
    st0 = status_fields("host_cycle", "host_link")
    drops = 0
    for m in range(minutes):
        time.sleep(60)
        st = status_fields("host_link", "host_cycle", "fault")
        if st.get("host_link") != "480":
            drops += 1
            log(f"idle {m + 1}/{minutes} min: host_link={st.get('host_link')} ({st.get('host_cycle')}) fault={st.get('fault')}")
    st1 = status_fields("host_cycle", "host_link", "fault")
    t_adb = wait_adb(30)
    good, detail = bm_check_adb()
    log(f"idle: {minutes} min done, link lost in {drops} samples, {st0.get('host_cycle')} -> {st1.get('host_cycle')}, "
        f"adb shell {'%.1fs' % t_adb if t_adb is not None else 'FAILED'}, check {'PASS' if good else 'FAIL ' + '; '.join(detail)}")
    rc, out = appliance("bm", "log", "4000", dev=UART, timeout=60)
    ports = [l.strip() for l in out.splitlines() if "port:" in l]
    if ports:
        log("   port transitions: " + " | ".join(ports[-10:]))
    return good and t_adb is not None


def main():
    global LOG
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("what", choices=["reboots", "concurrent", "fastboot", "idle", "all"])
    ap.add_argument("n", nargs="?", type=int)
    ap.add_argument("--size-mb", type=int, default=8)
    ap.add_argument("--boot-image", default=None, help="fastboot boot this image in each fastboot cycle")
    args = ap.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)
    LOG = OUT / f"bm-soak-{time.strftime('%Y%m%d-%H%M%S')}.log"
    print(f"log: {LOG}")
    if not adb_shell_ok(15):
        sys.exit("adb shell does not answer; is the rig up?")
    results = {}
    if args.what in ("reboots", "all"):
        results["reboots"] = soak_reboots(args.n if args.what == "reboots" and args.n else 100)
    if args.what in ("concurrent", "all"):
        results["concurrent"] = soak_concurrent(args.n if args.what == "concurrent" and args.n else 10, args.size_mb)
    if args.what in ("fastboot", "all"):
        results["fastboot"] = soak_fastboot(args.n if args.what == "fastboot" and args.n else 10, args.boot_image)
    if args.what in ("idle", "all"):
        results["idle"] = soak_idle(args.n if args.what == "idle" and args.n else 30)
    log("SUMMARY: " + ", ".join(f"{k}={'PASS' if v else 'FAIL'}" for k, v in results.items()))
    sys.exit(0 if all(results.values()) else 1)


if __name__ == "__main__":
    main()
