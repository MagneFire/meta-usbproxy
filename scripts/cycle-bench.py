#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.9"
# dependencies = ["pyserial"]
# ///
"""cycle-bench.py — paired adb push/pull throughput runs, md5-checked, with the
legacy-adb cycle estimate (4096 / rate) and, through the appliance, the
proxy's `[trace]` histograms (usb-proxy --cycle_trace). BARE-METAL.md M-1/M3.

    uv run scripts/cycle-bench.py --label appliance-accel-off
    uv run scripts/cycle-bench.py --label direct --no-log        # watch on the Mac
    uv run scripts/cycle-bench.py --label appliance-off-loop --shell-loop

Each run: a random file (--size-mb, default 20) pushed to the watch's tmpfs
and pulled back, md5 on both ends, N times (--runs). --shell-loop keeps an
`adb shell true` loop going during the transfers (the concurrent-stream
case). Unless --no-log, the appliance's usb-proxy log is read over the
console (UART or USB console, applib.serial_node) and the `[trace]` report
lines since the start are printed. Results are appended as a row to
~/.cache/appliance/cycle-bench.tsv. A watch load average above 1.0 at the
start of a run flags the run (DEVELOPMENT.md section 8: a busy watch gave
0.8 MB/s once).
"""
import argparse
import hashlib
import os
import pathlib
import re
import subprocess
import sys
import threading
import time

import applib as A

OUT = pathlib.Path(os.path.expanduser("~/.cache/appliance"))
REMOTE_DIR = "/tmp/cb"


def sh(*cmd, check=True, timeout=600):
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if check and r.returncode:
        sys.exit(f"{' '.join(cmd)} failed:\n{r.stdout}{r.stderr}")
    return r.stdout + r.stderr


def adb(*args, **kw):
    return sh("adb", *args, **kw)


def rate_of(text):
    m = re.search(r"([\d.]+) MB/s", text)
    return float(m.group(1)) if m else None


def watch_load():
    out = adb("shell", "cat", "/proc/loadavg", check=False)
    try:
        return float(out.split()[0])
    except (ValueError, IndexError):
        return None


class ShellLoop:
    def __init__(self):
        self.stop = threading.Event()
        self.count = 0
        self.fail = 0
        self.t = threading.Thread(target=self.run, daemon=True)

    def run(self):
        while not self.stop.is_set():
            r = subprocess.run(["adb", "shell", "true"], capture_output=True)
            self.count += 1
            if r.returncode:
                self.fail += 1
            time.sleep(0.2)

    def __enter__(self):
        self.t.start()
        return self

    def __exit__(self, *a):
        self.stop.set()
        self.t.join(timeout=5)


def fetch_trace(ser, since_marker):
    """`[trace]` lines from the proxy log after `since_marker` (a string the
    caller wrote into the log before the runs)."""
    out = A.lx_run(ser, "cat /var/volatile/log/usb-proxy.log", timeout=30)
    i = out.rfind(since_marker)
    tail = out[i:] if i >= 0 else out
    return [l for l in tail.splitlines() if "[trace]" in l]


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--label", required=True)
    ap.add_argument("--runs", type=int, default=3)
    ap.add_argument("--size-mb", type=int, default=20)
    ap.add_argument("--shell-loop", action="store_true")
    ap.add_argument("--no-log", action="store_true", help="no appliance console (direct connection)")
    args = ap.parse_args()

    OUT.mkdir(parents=True, exist_ok=True)
    local = OUT / f"rnd{args.size_mb}.bin"
    if not local.exists() or local.stat().st_size != args.size_mb << 20:
        local.write_bytes(os.urandom(args.size_mb << 20))
    md5 = hashlib.md5(local.read_bytes()).hexdigest()

    devs = [l for l in adb("devices").splitlines()[1:] if l.strip().endswith("device")]
    if len(devs) != 1:
        sys.exit(f"need exactly one adb device, have: {devs}")
    adb("shell", f"mkdir -p {REMOTE_DIR}")
    fs = adb("shell", f"df {REMOTE_DIR} | tail -1", check=False).split()
    print(f"remote {REMOTE_DIR} on {fs[0] if fs else '?'} (want tmpfs)")

    ser = None
    marker = f"cycle-bench {args.label} {int(time.time())}"
    if not args.no_log:
        ser = A.open_serial(timeout=1.0)
        A.lx_login(ser)
        A.lx_run(ser, f"echo '{marker}' >> /var/volatile/log/usb-proxy.log")

    rows = []
    loop = ShellLoop() if args.shell_loop else None
    if loop:
        loop.__enter__()
    try:
        for i in range(args.runs):
            load = watch_load()
            flag = " (BUSY)" if load is not None and load > 1.0 else ""
            t = time.time()
            push = adb("push", str(local), f"{REMOTE_DIR}/f.bin")
            push_s = time.time() - t
            rmd5 = adb("shell", f"md5sum {REMOTE_DIR}/f.bin").split()[0]
            back = OUT / "pulled.bin"
            t = time.time()
            pull = adb("pull", f"{REMOTE_DIR}/f.bin", str(back))
            pull_s = time.time() - t
            pmd5 = hashlib.md5(back.read_bytes()).hexdigest()
            pr, lr = rate_of(push), rate_of(pull)
            ok = (rmd5 == md5) and (pmd5 == md5)
            rows.append((pr, lr, ok, load))
            print(f"run {i+1}: load {load}{flag}  push {pr} MB/s ({push_s:.1f}s)  pull {lr} MB/s ({pull_s:.1f}s)  "
                  f"cycle~ {4096/pr/1e6*1e3 if pr else 0:.3f}/{4096/lr/1e6*1e3 if lr else 0:.3f} ms  md5 {'OK' if ok else 'MISMATCH'}")
    finally:
        if loop:
            loop.__exit__(None, None, None)
            print(f"shell loop: {loop.count} commands, {loop.fail} failures")

    if ser:
        for l in fetch_trace(ser, marker):
            print(l)
        ser.close()

    good = [r for r in rows if r[2]]
    mp = sum(r[0] for r in good) / len(good) if good else 0
    ml = sum(r[1] for r in good) / len(good) if good else 0
    with open(OUT / "cycle-bench.tsv", "a") as f:
        f.write(f"{time.strftime('%Y-%m-%d %H:%M')}\t{args.label}\t{args.size_mb}MB\truns={len(rows)}\t"
                f"shell_loop={args.shell_loop}\tpush={mp:.2f}\tpull={ml:.2f}\tmd5_ok={all(r[2] for r in rows)}\n")
    print(f"{args.label}: push {mp:.2f} MB/s (cycle ~{4096/mp/1e3 if mp else 0:.3f} ms), "
          f"pull {ml:.2f} MB/s (cycle ~{4096/ml/1e3 if ml else 0:.3f} ms)")


if __name__ == "__main__":
    main()
