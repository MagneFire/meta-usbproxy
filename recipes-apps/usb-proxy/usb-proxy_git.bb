SUMMARY = "USB proxy — raw-gadget man-in-the-middle for USB traffic"
DESCRIPTION = "Single-purpose USB proxy that sits between a USB host and device \
using raw-gadget on the sunxi musb UDC. This recipe builds the MagneFire fork's \
opi branch, which carries the Orange Pi Zero / sunxi musb fixes."
HOMEPAGE = "https://github.com/MagneFire/usb-proxy"
LICENSE = "Apache-2.0"
LIC_FILES_CHKSUM = "file://LICENSE;md5=86d3f3a95c324c9479bd8986968f4327"

DEPENDS = "libusb1 jsoncpp"

SRC_URI = "git://github.com/MagneFire/usb-proxy.git;protocol=https;branch=opi \
           file://config.json \
           file://usb-proxy-run \
           file://power-tune \
           file://usb-flash-mode \
"
# opi branch HEAD (carries the sunxi musb fixes, the NO_DEVICE _exit-on-
# disconnect fix, drain-before-exit so a `fastboot boot` OKAY still reaches
# the host as the device drops off the bus, the condvar/fast-path latency
# work and the adb_ack_accel throughput feature, plus the idle-power work:
# the endpoint write loop now sleeps instead of polling at 1kHz, and
# --power_hook/--power_idle_ms drive power-tune's active/idle modes; and
# always-exit-on-device-loss: ep0-path NO_DEVICE, a direct-exit hotplug
# callback and a 1 Hz devtmpfs-node liveness check, so a device that
# vanishes mid-enumeration can no longer leave a stuck proxy needing a
# replug; and the in-process device wait: the proxy polls for a device
# itself (100 ms, sysfs only) with a --settle_ms debounce, and stamps its
# milestone log lines with the dmesg clock -- see DEVELOPMENT.md 8); the
# CDC-ACM console; and the persistent gadget (one fixed gadget, the
# device's adb/fastboot bulk endpoints bridged onto it, no host
# re-enumeration on device changes -- DEVELOPMENT.md 7).
# Bump to advance.
SRCREV = "6ea542f02c3f1875f301dc6c68fe4085d5a2f0cb"
PV = "1.0+git${SRCPV}"

S = "${WORKDIR}/git"

# The upstream Makefile hardcodes `g++`, ignores LDFLAGS, and pulls in Lua when
# present. It also includes jsoncpp as <jsoncpp/json/json.h> (the Debian header
# layout). Build explicitly with the cross toolchain, honor LDFLAGS (QA), skip
# Lua, and provide a compat include dir so <jsoncpp/json/json.h> resolves to
# OE's jsoncpp headers.
do_configure() {
    rm -rf ${WORKDIR}/jsoncpp-compat
    install -d ${WORKDIR}/jsoncpp-compat
    ln -sfn ${STAGING_INCDIR} ${WORKDIR}/jsoncpp-compat/jsoncpp
}

do_compile() {
    ${CXX} ${CXXFLAGS} -I${WORKDIR}/jsoncpp-compat \
        usb-proxy.cpp host-raw-gadget.cpp device-libusb.cpp proxy.cpp misc.cpp \
        power-policy.cpp console-acm.cpp console-shell.cpp gadget-idle.cpp \
        gadget-fixed.cpp bridge.cpp \
        ${LDFLAGS} -lusb-1.0 -pthread -ljsoncpp -lutil \
        -o usb-proxy
}

do_install() {
    install -d ${D}${bindir}
    install -m 0755 ${S}/usb-proxy        ${D}${bindir}/usb-proxy
    install -m 0755 ${WORKDIR}/usb-proxy-run ${D}${bindir}/usb-proxy-run
    install -m 0755 ${WORKDIR}/power-tune     ${D}${bindir}/power-tune
    install -m 0755 ${WORKDIR}/usb-flash-mode ${D}${bindir}/usb-flash-mode

    install -d ${D}${sysconfdir}/usb-proxy
    install -m 0644 ${WORKDIR}/config.json ${D}${sysconfdir}/usb-proxy/config.json
}

FILES:${PN} = "${bindir} ${sysconfdir}/usb-proxy"
