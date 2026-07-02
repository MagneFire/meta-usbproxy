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
"
# opi branch HEAD (carries the sunxi musb fixes + the NO_DEVICE _exit-on-
# disconnect fix). Bump to advance.
SRCREV = "ce92f49975e32ee38bdf3b008b805a901904aaa7"
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
        ${LDFLAGS} -lusb-1.0 -pthread -ljsoncpp \
        -o usb-proxy
}

do_install() {
    install -d ${D}${bindir}
    install -m 0755 ${S}/usb-proxy        ${D}${bindir}/usb-proxy
    install -m 0755 ${WORKDIR}/usb-proxy-run ${D}${bindir}/usb-proxy-run
    install -m 0755 ${WORKDIR}/power-tune     ${D}${bindir}/power-tune

    install -d ${D}${sysconfdir}/usb-proxy
    install -m 0644 ${WORKDIR}/config.json ${D}${sysconfdir}/usb-proxy/config.json
}

FILES:${PN} = "${bindir} ${sysconfdir}/usb-proxy"
