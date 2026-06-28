SUMMARY = "USB-proxy appliance SD image — u-boot + initramfs-bundled kernel, no rootfs partition"
LICENSE = "GPL-3.0-only"

inherit core-image

# The runtime root filesystem is the initramfs (usbproxy-initramfs) bundled into
# the kernel, so this image lays down ONLY the bootloader + a small FAT boot
# partition — there is no ext4 rootfs partition. This image's own rootfs is
# unused (the wks below has no "part /").
IMAGE_INSTALL = ""
IMAGE_FEATURES = ""
IMAGE_LINGUAS = ""
NO_RECOMMENDATIONS = "1"

# Put the *bundled* kernel (uImage with the initramfs inside) on /boot as
# "uImage" so the stock meta-sunxi boot.scr loads it. The plain uImage has no
# initramfs, so we must select the .initramfs variant explicitly and rename it.
IMAGE_BOOT_FILES = "${KERNEL_IMAGETYPE}-initramfs-${MACHINE}.bin;${KERNEL_IMAGETYPE} \
                    boot.scr \
                    ${@d.getVar('KERNEL_DEVICETREE','').split('/')[-1]}"

# SD layout: u-boot SPL + FAT /boot only (no rootfs partition).
# NOTE: must override WKS_FILES (plural) — meta-sunxi's sunxi.inc sets it and the
# wic class resolves it ahead of WKS_FILE (singular). Setting only WKS_FILE is
# silently ignored and the stock sunxi-sdcard-image.wks (boot + ext4 rootfs) is
# used, giving a dead ~80MB rootfs partition we never mount.
WKS_FILES = "usbproxy-sdcard.wks.in"
IMAGE_FSTYPES = "wic.gz wic.bmap"
