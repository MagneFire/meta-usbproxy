SUMMARY = "usb-proxy appliance root filesystem — runs from RAM, bundled into the kernel as an initramfs"
LICENSE = "GPL-3.0-only"

inherit core-image

# Same appliance content as before (BusyBox init + usb-proxy), but delivered as
# a RAM rootfs: a gzipped cpio the kernel unpacks into RAM. There is no rootfs
# partition and nothing persists across boots (power-loss proof). No
# read-only-rootfs needed — RAM is volatile by nature.
IMAGE_INSTALL = "packagegroup-core-boot usb-proxy"
IMAGE_FEATURES:remove = "package-management"
# Passwordless root on the serial console for recovery.
IMAGE_FEATURES += "empty-root-password allow-empty-password allow-root-login"

IMAGE_LINGUAS = ""
# Keep kernel-modules (a meta-sunxi machine RRECOMMENDS) OUT of the initramfs —
# otherwise it pulls virtual/kernel and creates a circular dependency with
# INITRAMFS_IMAGE_BUNDLE (which bundles this image into the kernel).
NO_RECOMMENDATIONS = "1"

# Bundled-initramfs deliverable: a single gzipped cpio.
IMAGE_FSTYPES = "cpio.gz"
INITRAMFS_MAXSIZE = "262144"

# do_bundle_initramfs looks for ${INITRAMFS_IMAGE}-${MACHINE}.cpio.gz; drop the
# default ".rootfs" IMAGE_NAME_SUFFIX so the deployed name matches (as Yocto's
# own *-initramfs images do).
IMAGE_NAME_SUFFIX = ""
