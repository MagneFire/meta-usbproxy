SUMMARY = "Minimal read-only USB-proxy appliance image for Orange Pi Zero"
LICENSE = "GPL-3.0-only"

inherit core-image

# read-only rootfs: nothing persists, power-loss safe. Writable runtime dirs
# (/var/volatile etc.) are overlaid on tmpfs by initscripts' volatile handling.
IMAGE_FEATURES += "read-only-rootfs"
IMAGE_FEATURES:remove = "package-management"

# Just enough to boot under BusyBox init, plus the proxy. packagegroup-core-boot
# pulls busybox init + mdev (via INIT_MANAGER=mdev-busybox), base-files, etc.
IMAGE_INSTALL = "packagegroup-core-boot usb-proxy"

# Smallest possible: no locales, no recommended-package bloat.
IMAGE_LINGUAS = ""
NO_RECOMMENDATIONS = "1"

# Flashable SD-card image (+ bmap for fast flashing). Uses meta-sunxi's default
# sunxi-sdcard-image.wks.in (single bootable rootfs partition).
IMAGE_FSTYPES = "wic.gz wic.bmap"

IMAGE_OVERHEAD_FACTOR = "1.0"
IMAGE_ROOTFS_EXTRA_SPACE = "4096"
