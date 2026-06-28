# meta-usbproxy

A minimal, **stateless** Yocto (scarthgap) distro + image that turns an
**Orange Pi Zero** (Allwinner H2+/sun8i-h3) into a single-purpose
[usb-proxy](https://github.com/MagneFire/usb-proxy) appliance. It does one thing:
run `usb-proxy` on the sunxi musb UDC, as fast as possible after power-on. The
whole system runs from a RAM initramfs — nothing is written to the SD at runtime,
so there's no filesystem to corrupt on power loss.

## Design

- **Runs entirely from RAM.** The rootfs is an initramfs *bundled into the kernel*
  (`INITRAMFS_IMAGE_BUNDLE`); the SD holds only U-Boot + a small FAT `/boot` —
  **no rootfs partition**, nothing mounted from the card at runtime, power-loss
  safe. `config.json` is baked in (`reset_device_before_proxy: false`).
- **No systemd, no udev, no network.** `INIT_MANAGER = "mdev-busybox"` (BusyBox
  init + mdev + devtmpfs); init respawns `usb-proxy` directly from `/etc/inittab`.
  A serial getty on `ttyS0` (115200) is kept for recovery.
- **musb bug fixed in the kernel, not at runtime.** The sunxi musb bulk-OUT
  packet-loss bug (ADB "offline") is patched into `musb_gadget.c` (`0001-musb-…`)
  — the in-tree counterpart to the old out-of-tree `musbfix.ko` kprobe. The
  micro-USB enumerates as a gadget via a DT trim + the megous peripheral-mode
  patch (`0002`/`0003`). No vermagic fragility.
- **`raw_gadget` + musb built into the kernel (`=y`)**, so `/dev/raw-gadget`
  exists at boot with nothing to modprobe.
- **Trimmed kernel.** Single-purpose H3 config drops the IP-stack drivers,
  wifi/BT, display, audio, media, RAID and on-disk filesystems — ~3.2 MB uImage,
  0 modules (`usbproxy-trim.cfg`). `CONFIG_NET` core stays because libusb's
  hotplug uses netlink.
- **Low power.** A boot-time `power-tune` offlines 2 of the 4 A7 cores and turns
  off the unused Ethernet PHY's RJ45 LEDs (already gated/in-reset by default; the
  LEDs just needed the syscon polarity bit).
- **Fast U-Boot**: `bootdelay=0`, no USB/network boot scan, Ethernet driver
  dropped — boots straight from the SD card in well under a second.

## Layout

```
conf/layer.conf                            layer definition (scarthgap)
conf/distro/usbproxy.conf                  the "usbproxy" distro (mdev-busybox, trimmed features)
recipes-core/images/usbproxy-initramfs.bb  the RAM rootfs, bundled into the kernel
recipes-core/images/usbproxy-image.bb      bootable SD image (u-boot + FAT /boot, no rootfs part)
wic/usbproxy-sdcard.wks.in                 SD layout: u-boot SPL + FAT /boot only
recipes-apps/usb-proxy/                     usb-proxy recipe + launcher + power-tune + config.json
recipes-kernel/linux/                       musb/DT patches + kernel config + trim fragments (bbappend)
recipes-bsp/u-boot/                         bootdelay=0, no-USB-boot, no-EMAC, bigger bootm-len (bbappend)
recipes-core/busybox-inittab/               adds power-tune + usb-proxy respawn to /etc/inittab (bbappend)
recipes-core/busybox/                       enables the devmem applet used by power-tune (bbappend)
recipes-support/libusb/                     builds libusb without udev → netlink hotplug (bbappend)
scripts/host-deps.sh                        install Yocto build deps (Debian/Ubuntu)
scripts/setup-build.sh                      clone layers @scarthgap + write build conf (+ rm_work)
```

The build pulls the usb-proxy source from the MagneFire fork, branch `opi`
(`SRCREV` pinned in `recipes-apps/usb-proxy/usb-proxy_git.bb`).

## Build (on macOS via OrbStack)

Yocto needs a Linux host. We build at native arm64 speed in the OrbStack Debian
machine. Keep all build state on the container's **native** filesystem
(`~/yocto/...`), never on the macOS-shared `/Users` mount.

```sh
# This repo lives at /Users/<you>/Downloads/meta-usbproxy and is visible inside
# the container at the same path.

# 1. Install build dependencies (once)
orb run bash /Users/<you>/Downloads/meta-usbproxy/scripts/host-deps.sh

# 2. Clone layers @scarthgap and generate build/conf
orb run bash /Users/<you>/Downloads/meta-usbproxy/scripts/setup-build.sh

# 3. Build (first build takes a few hours; arm64-host sstate is not on the
#    public mirror so native tools are compiled locally)
orb run bash -lc 'export LANG=en_US.UTF-8 LC_ALL=en_US.UTF-8; \
  cd ~/yocto/usbproxy && source layers/poky/oe-init-build-env build && \
  bitbake usbproxy-image'
```

Output: `~/yocto/usbproxy/tmp/deploy/images/orange-pi-zero/usbproxy-image-orange-pi-zero.rootfs.wic.gz`
(+ `.wic.bmap`).

## Flash & run

```sh
# Fastest (uses the bmap):  bmaptool copy <image>.wic.gz /dev/<sdcard>
# Or:                       zcat <image>.wic.gz | sudo dd of=/dev/<sdcard> bs=4M
```

Boot the Orange Pi Zero. On the serial console you'll see U-Boot hand off with no
delay and `usb-proxy` start within a second or two. Plug the target USB device
into the OTG port; from a USB host (e.g. this Mac) `adb devices` should show
`device` (not `offline`), confirming the baked-in musb fix. `usb-proxy` logs to
`/var/volatile/log/usb-proxy.log` (tail it over the serial getty).

## Build host notes

`setup-build.sh` writes `build/conf/local.conf` (MACHINE `orange-pi-zero`, DISTRO
`usbproxy`, `TMPDIR`/`DL_DIR`/`SSTATE_DIR` under `~/yocto/usbproxy`) and
`build/conf/bblayers.conf` with: poky `meta`/`meta-poky`, meta-openembedded
`meta-oe`/`meta-python`, meta-arm `meta-arm`/`meta-arm-toolchain`, `meta-sunxi`,
and this layer.

## Licence

GPL-3.0-only (see `COPYING`). usb-proxy itself is Apache-2.0.
