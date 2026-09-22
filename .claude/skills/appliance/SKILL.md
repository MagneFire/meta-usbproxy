---
name: appliance
description: Deploy to and verify the Orange Pi usb-proxy appliance — RAM-only binary swap, RAM-only whole-image boot, or flashing the card over the serial console or over USB (U-Boot DFU) — and the checks that prove it took. Use whenever a change must reach the board, when "did the flash work?", or when the board looks wrong after a deploy.
---

# Deploying to the appliance

Everything goes through `scripts/appliance.py` (run with `uv run`, from the
repo root). Each subcommand ends with the same `check`; a deploy without a
passing check is not done.

## Pick the tier

| What changed | Command | Survives reboot? | Time |
|---|---|---|---|
| usb-proxy source (`/Users/darrel/Downloads/usb-proxy`) | `appliance.py swap` | no (RAM rootfs) | ~2 min |
| kernel, DT, initramfs contents, launcher, config.json, inittab | `appliance.py boot-ram` | no (card untouched) | ~3 min (750000 baud), ~12 min if U-Boot lacks the baud option |
| the release: anything, for keeps | `appliance.py flash --adb` | yes | as above + reset |
| the release, no UART dongle | `appliance.py flash --usb --adb` | yes | ~20 s (U-Boot DFU over the OTG port; needs `dfu-util` and a card that already has the DFU U-Boot/boot.scr — flash those once over UART with `flash --uboot`) |
| U-Boot / SPL (DRAM clock, WDT, this fragment) | `appliance.py flash --uboot` | yes | + ~1 min |

`swap` builds the devtool workspace (`devtool modify --no-extract usb-proxy
/Users/darrel/Downloads/usb-proxy` must be active; without it the pinned
build from the deploy dir is sent). `boot-ram` and `flash` take the deploy
dir's `uImage-initramfs-orange-pi-zero.bin` after a `bitbake usbproxy-image`
in OrbStack (DEVELOPMENT.md §2). `boot-ram`/`flash` transfer at 750000 baud
by default (the card's U-Boot has had `CONFIG_SYS_LOADS_BAUD_CHANGE` since
2026-09-09) and fall back to 115200 when a U-Boot does not offer the switch;
`--baud 0` forces 115200. Never another fast rate: the UART divisor rounds it
and the board strands waiting for ENTER.

## What `check` must print

All PASS: SPL DRAM banner **256 MiB** (512 = misdetection, nothing after it
counts), watchdog armed, no oops, login reached, image build stamp equals the
deploy dir's, usb-proxy md5 equals the build's, exactly one real
`/usr/bin/usb-proxy` running, pstore empty, and with `--adb` the watch listed
as `device` on the Mac. `appliance.py check --adb` runs it on a live board;
`--reboot` grades a fresh boot first. Console transcripts land in
`~/.cache/appliance/`.

## Before a release flash

- `devtool reset usb-proxy`, then `bitbake usbproxy-image`; the SRCREV in
  `recipes-apps/usb-proxy/usb-proxy_git.bb` must be the pushed `opi` commit.
- If a change "did not take": sstate. `bitbake -c cleansstate <recipe>` and
  rebuild (DEVELOPMENT.md §8). `check` catches this: the build stamp will not
  match.
- Not while an adb/fastboot session or TWRP is in use on the watch: every
  tier reboots or respawns the proxy.

## Gotchas (each cost hours once)

- **Only one USB-UART dongle**, or set `PI_DEV`; the node name changes.
  Without a dongle the scripts fall back to the USB console
  (`/dev/cu.usbmodem*`, the CDC-ACM function on the proxy port): shell-level
  things work there (`check`, `swap`, `flash --usb`), U-Boot-level things
  (`boot-ram`, serial `flash`, `--reboot` grading) do not. With the
  persistent gadget (`persistent_gadget: true`, the appliance default since
  2026-09-12) the node is always `/dev/cu.usbmodemUSBPROXY011` and survives
  adb↔fastboot transitions; `adb devices`/`fastboot devices` then show the
  fixed serial `USBPROXY01`, not the watch's. Only a proxy respawn
  (`swap`, `kill -9`) re-enumerates it.
- **Kernel file = `uImage-initramfs-*.bin`.** The plain `uImage-*.bin` hangs at
  "Starting kernel" and needs a power-cycle; the scripts refuse it.
- **Restart the proxy with `kill -9`**, never SIGTERM (graceful path hangs on
  the connected host). `swap` does this.
- **A respawn can wedge the Mac's USB port** (stale device object): `adb`
  sees nothing though the board is fine. `check --adb` runs
  `scripts/mac-usb-unwedge.py` once; run it by hand before any replug.
- **Replugging the Mac↔Pi cable power-cycles the Pi** and erases the RAM log
  and any swapped binary.
- **`USBPROXY01 offline` after a watch reboot, with `does not fit the
  adb/fastboot template` and `18d1:0afe` in the log**: the watch came up in
  usb-moded's charging-only mode, not a proxy fault (DEVELOPMENT.md §7).
  Reseat the watch; that power-cycles the board too (swap and log gone). A
  USB-A port power-cycle does not clear it.
- **U-Boot stays reachable** even when Linux hangs: `--catch` breaks
  `bootdelay=0`. Only a kernel that never returns to U-Boot (kernel-only
  uImage, hung init) needs a physical power-cycle first.
- **BusyBox shell**: `head -n 3` not `head -3`; no `base64`; lines over
  1024 chars are silently truncated.
- **Power measurements**: not with a single meter reading (DEVELOPMENT.md §10).

Why each of these is so: DEVELOPMENT.md §5–§9.
