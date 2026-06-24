FILESEXTRAPATHS:prepend := "${THISDIR}/files:"

# Speed up boot: bootdelay=0 and stop U-Boot scanning USB / network for boot
# media (the device boots only from the SD card). Delivered as a defconfig
# fragment merged into U-Boot's .config after the normal do_configure.
SRC_URI:append = " file://usbproxy-uboot.cfg"

do_configure:append() {
    if [ -e "${WORKDIR}/usbproxy-uboot.cfg" ] && [ -e "${B}/.config" ]; then
        cat ${WORKDIR}/usbproxy-uboot.cfg >> ${B}/.config
        oe_runmake -C ${S} O=${B} olddefconfig
    fi
}
