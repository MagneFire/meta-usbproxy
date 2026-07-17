#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.9"
# dependencies = ["pyusb"]
# ///
"""
mac-usb-unwedge.py — clear a wedged macOS USB port without rebooting the Mac.

Runs on the Mac. Needs the libusb C library (Homebrew: `brew install libusb`).

Symptom (seen repeatedly with the usb-proxy appliance): after rapid gadget
detach/re-attach cycles (proxy kill -9 respawns, device churn), macOS keeps a
stale IOUSBHostDevice object for the gadget. The port then looks alive but is
dead: descriptor reads "succeed" because macOS serves them from its cache,
while anything that must actually reach the bus fails with LIBUSB_ERROR_IO,
`system_profiler SPUSBDataType` prints nothing, adb/fastboot can't talk to the
device, and neither physical replugs nor 30 s gadget-side detaches clear it.

The fix (found 2026-07-17, see meta-usbproxy/MUSB-BULK-OUT.md §7b/7c): issue a
libusb_reset_device against the stale object. That forces macOS to drop it and
freshly re-enumerate the port. No Mac reboot needed.

Usage:

    uv run scripts/mac-usb-unwedge.py              # probe all devices, reset wedged ones
    uv run scripts/mac-usb-unwedge.py 18d1:d001    # reset this VID:PID unconditionally

Detection probe: GET_STATUS(device) — 2 bytes, side-effect free, and never
served from the macOS cache, so it fails with an I/O error exactly when the
port is wedged. Hubs and healthy devices answer it and are left alone.

Note: on a healthy port the reset still bounces the device (it re-enumerates),
so only pass an explicit VID:PID if you mean it. Through the usb-proxy this is
safe for fastboot/TWRP/Wear OS sessions since the proxy stopped forwarding
duplicate SET_CONFIGURATIONs (usb-proxy 56aa36a); the proxied device stays up.
"""
import sys

import usb.core
import usb.util


def get_status_ok(dev):
    try:
        dev.ctrl_transfer(0x80, 0x00, 0, 0, 2, timeout=2000)
        return True
    except usb.core.USBError:
        return False


def reset(dev, why):
    ident = f"{dev.idVendor:04x}:{dev.idProduct:04x} (bus {dev.bus} addr {dev.address})"
    try:
        dev.reset()
        print(f"reset OK   {ident} — {why}")
        return True
    except usb.core.USBError as e:
        print(f"reset FAIL {ident} — {e}")
        return False


def main():
    if len(sys.argv) > 1:
        vid, pid = (int(x, 16) for x in sys.argv[1].split(":"))
        dev = usb.core.find(idVendor=vid, idProduct=pid)
        if dev is None:
            print(f"no device {vid:04x}:{pid:04x} found")
            return 1
        return 0 if reset(dev, "requested explicitly") else 1

    wedged = healthy = 0
    for dev in usb.core.find(find_all=True):
        if dev.bDeviceClass == 0x09:  # hub
            continue
        ident = f"{dev.idVendor:04x}:{dev.idProduct:04x} (bus {dev.bus} addr {dev.address})"
        if get_status_ok(dev):
            print(f"healthy    {ident}")
            healthy += 1
        else:
            wedged += 1
            reset(dev, "GET_STATUS failed (wedge signature)")
    if wedged == 0:
        print(f"no wedged devices ({healthy} healthy)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
