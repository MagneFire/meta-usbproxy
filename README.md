# meta-usbproxy

A minimal, **read-only** Yocto (scarthgap) distro + image that turns an
**Orange Pi Zero** (Allwinner H2+/sun8i-h3) into a single-purpose
[usb-proxy](https://github.com/MagneFire/usb-proxy) appliance. It does one thing:
run `usb-proxy` on the sunxi musb UDC, as fast as possible after power-on.

## Design

- **No systemd, no udev, no network.** `INIT_MANAGER = "mdev-busybox"`; BusyBox
  init respawns `usb-proxy` directly from `/etc/inittab`. A serial getty on
  `ttyS0` (115200) is kept for recovery.
- **musb bug fixed in the kernel, not at runtime.** The sunxi musb bulk-OUT
  packet-loss bug (ADB "offline") is fixed by patching `musb_gadget.c`
  (`recipes-kernel/linux/files/0001-musb-...patch`) — the in-tree counterpart to
  the old out-of-tree `musbfix.ko` kprobe. No vermagic fragility, no rebuild on
  kernel update.
- **`raw_gadget` + musb built into the kernel (`=y`)**, so `/dev/raw-gadget`
  exists at boot with nothing to modprobe.
- **Read-only rootfs** (`read-only-rootfs`); writable runtime dirs are tmpfs.
  Power-loss safe; `config.json` is baked in (`reset_device_before_proxy: false`).
- **Fast U-Boot**: `bootdelay=0` and USB/network boot scanning disabled — boots
  straight from the SD card.

## Layout

```
conf/layer.conf                         layer definition (scarthgap)
conf/distro/usbproxy.conf               the "usbproxy" distro (mdev-busybox, trimmed features)
recipes-core/images/usbproxy-image.bb   the read-only appliance image
recipes-apps/usb-proxy/                  usb-proxy recipe + launcher + config.json
recipes-kernel/linux/                    musb patch + kernel config fragment (bbappend)
recipes-core/busybox/                    custom /etc/inittab (bbappend)
recipes-bsp/u-boot/                      bootdelay=0 + no-USB-boot fragment (bbappend)
scripts/host-deps.sh                     install Yocto build deps (Debian/Ubuntu)
scripts/setup-build.sh                   clone layers @scarthgap + write build conf
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
