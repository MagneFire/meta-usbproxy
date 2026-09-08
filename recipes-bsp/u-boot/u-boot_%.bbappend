FILESEXTRAPATHS:prepend := "${THISDIR}/files:"

# Speed up boot: bootdelay=0 and stop U-Boot scanning USB / network for boot
# media (the device boots only from the SD card). Delivered as a defconfig
# fragment merged into U-Boot's .config after the normal do_configure.
#
# 0001-sunxi-dw-dram-...: the SPL's DRAM size auto-detection used one write
# pair per aliasing test and intermittently reported 512 MiB on this 256 MiB
# board; the mirrored upper half then let ramoops overwrite the kernel's
# mem_map and free_initmem() oopsed. Port of upstream's 16-word pattern test
# (v2025.07, H616-only upstream) to the H3 driver. See usbproxy-uboot.cfg and
# DEVELOPMENT.md section 9.
SRC_URI:append = " \
    file://0001-sunxi-dw-dram-use-pattern-based-size-detection.patch \
    file://usbproxy-uboot.cfg \
"

do_configure:append() {
    if [ -e "${WORKDIR}/usbproxy-uboot.cfg" ] && [ -e "${B}/.config" ]; then
        cat ${WORKDIR}/usbproxy-uboot.cfg >> ${B}/.config
        oe_runmake -C ${S} O=${B} olddefconfig
    fi
}
