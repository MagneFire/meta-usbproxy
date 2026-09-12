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
    file://0002-sunxi-board-usb-init-probe-musb-gadget.patch \
    file://usbproxy-uboot.cfg \
    file://usbproxy-boot.cmd \
"

# usbproxy-boot.cmd: the appliance's own boot script. It cannot simply be a
# files/boot.cmd here: meta-sunxi's FILESEXTRAPATHS:prepend:sunxi is applied
# after ours, so their files/arm/boot.cmd is found first (checked with
# `bitbake -e u-boot | grep ^FILESPATH=`). Instead the task below regenerates
# ${UBOOT_ENV_BINARY} (boot.scr) from our file after meta-sunxi's do_compile
# made it from theirs, before it is installed/deployed. The script skips the
# uImage CRC and the zImage probes and boots with maxcpus=2; the reasons are
# in the file. Changing it needs only `appliance.py flash` (the script lives
# on the FAT partition), not a U-Boot reflash.
do_usbproxy_bootscr() {
    ${B}/tools/mkimage -C none -A arm -T script \
        -d ${WORKDIR}/usbproxy-boot.cmd ${WORKDIR}/${UBOOT_ENV_BINARY}
}
addtask usbproxy_bootscr after do_compile before do_install do_deploy

do_configure:append() {
    if [ -e "${WORKDIR}/usbproxy-uboot.cfg" ] && [ -e "${B}/.config" ]; then
        cat ${WORKDIR}/usbproxy-uboot.cfg >> ${B}/.config
        oe_runmake -C ${S} O=${B} olddefconfig
    fi
}
