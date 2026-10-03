SUMMARY = "usb-proxy appliance root filesystem — runs from RAM, bundled into the kernel as an initramfs"
LICENSE = "GPL-3.0-only"

inherit core-image

# Same appliance content as before (BusyBox init + usb-proxy), but delivered as
# a RAM rootfs: a gzipped cpio the kernel unpacks into RAM. There is no rootfs
# partition and nothing persists across boots (power-loss proof). No
# read-only-rootfs needed — RAM is volatile by nature.
IMAGE_INSTALL = "packagegroup-core-boot usb-proxy"
# memtester (meta-oe): hours-long DRAM test on the running appliance, to rule
# DRAM in or out for the boot-time corruption (DEVELOPMENT.md section 9).
# `memtester 64M 20` with the proxy idle; ~130 KB, nothing runs it by itself.
IMAGE_INSTALL += "memtester"
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

# do_bundle_initramfs looks for ${INITRAMFS_IMAGE}-${MACHINE}.cpio.gz; drop the
# default ".rootfs" IMAGE_NAME_SUFFIX so the deployed name matches (as Yocto's
# own *-initramfs images do).
IMAGE_NAME_SUFFIX = ""

# /etc/buildinfo: which build is this? The RAM rootfs has no other identity
# (/etc/version is the fixed reproducible-build stamp), and a deploy that
# "did not take" is otherwise indistinguishable from one that did.
# scripts/appliance.py check compares the meta-usbproxy revision and the
# DATETIME stamp (the one in the deploy filenames) against the build tree.
inherit image-buildinfo

# Size (2026-10-03): the SPL reads this rootfs on every boot, so drop the
# glibc pieces nothing here can use: no DNS or NIS (libresolv, libnsl,
# libnss_dns, libanl, libnss_compat) and no locale switching
# (libBrokenLocale). libnss_files stays for the login name lookup.
usbproxy_drop_glibc_extras() {
    rm -f ${IMAGE_ROOTFS}${base_libdir}/libresolv.so.* \
          ${IMAGE_ROOTFS}${base_libdir}/libnsl.so.* \
          ${IMAGE_ROOTFS}${base_libdir}/libnss_dns.so.* \
          ${IMAGE_ROOTFS}${base_libdir}/libanl.so.* \
          ${IMAGE_ROOTFS}${base_libdir}/libnss_compat.so.* \
          ${IMAGE_ROOTFS}${base_libdir}/libBrokenLocale.so.*
}
ROOTFS_POSTPROCESS_COMMAND += "usbproxy_drop_glibc_extras;"
IMAGE_BUILDINFO_VARS = "DISTRO DISTRO_VERSION MACHINE DATETIME"
