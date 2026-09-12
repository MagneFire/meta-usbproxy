#!/usr/bin/env -S uv run --script
# /// script
# dependencies = ["pyserial"]
# ///
"""usb-console.py — interactive terminal on the appliance's USB console.

The console is a CDC-ACM function usb-proxy adds to the gadget on the OTG
port (meta-usbproxy config.json `usb_console`), so it shows up on the Mac as
/dev/cu.usbmodem* and gives the same root shell the UART getty gives — over
the cable that is already there. Two things differ from the UART, and this
script exists to smooth them over:

  * the node comes and goes: it lives with the proxied device (adb<->fastboot
    transitions drop it for 10-25 s) and, between devices, with the idle
    console-only gadget. The script waits for a node and reconnects when it
    vanishes, so one terminal window follows the appliance through it all.
  * each usb-proxy instance has its own shell, so a reconnect lands in a fresh
    prompt (a `tail -f` does not survive a transition).

Usage: scripts/usb-console.py [node]      (PI_DEV also works; ~. or Ctrl-] quits)
"""
import glob
import os
import select
import sys
import termios
import time
import tty

import serial


def pick_node(arg):
    if arg:
        return arg
    if os.environ.get("PI_DEV"):
        return os.environ["PI_DEV"]
    nodes = sorted(glob.glob("/dev/cu.usbmodem*"))
    return nodes[0] if len(nodes) == 1 else (nodes if nodes else None)


def wait_for_node(arg):
    said = False
    while True:
        n = pick_node(arg)
        if isinstance(n, list):
            sys.exit("several /dev/cu.usbmodem* nodes, pass one: " + " ".join(n))
        if n and os.path.exists(n):
            return n
        if not said:
            print("[usb-console] waiting for /dev/cu.usbmodem* ...", file=sys.stderr)
            said = True
        time.sleep(0.5)


def session(node):
    """One connection: pump bytes both ways until the node dies or the user
    types Ctrl-]. Returns False when the user quit."""
    try:
        ser = serial.Serial(node, 115200, timeout=0)
    except serial.SerialException as e:
        print(f"[usb-console] {node}: {e}", file=sys.stderr)
        time.sleep(0.5)
        return True
    print(f"[usb-console] connected to {node} (Ctrl-] to quit)", file=sys.stderr)
    fd = sys.stdin.fileno()
    saved = termios.tcgetattr(fd)
    tty.setraw(fd)
    try:
        ser.write(b"\n")
        while True:
            r, _, _ = select.select([fd, ser.fileno()], [], [], 0.5)
            if fd in r:
                data = os.read(fd, 1024)
                if not data or b"\x1d" in data:  # Ctrl-]
                    return False
                ser.write(data)
            if ser.fileno() in r:
                try:
                    data = ser.read(4096)
                except (serial.SerialException, OSError):
                    print("\r\n[usb-console] node gone, waiting for it to return", file=sys.stderr)
                    return True
                if data:
                    os.write(sys.stdout.fileno(), data)
            if not os.path.exists(node):
                print("\r\n[usb-console] node gone, waiting for it to return", file=sys.stderr)
                return True
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, saved)
        try:
            ser.close()
        except Exception:
            pass


def main():
    arg = sys.argv[1] if len(sys.argv) > 1 else None
    while True:
        node = wait_for_node(arg)
        if not session(node):
            print("\r\n[usb-console] bye", file=sys.stderr)
            return
        time.sleep(0.3)


if __name__ == "__main__":
    main()
