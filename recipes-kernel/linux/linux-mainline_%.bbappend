FILESEXTRAPATHS:prepend := "${THISDIR}/files:"

# Orange Pi Zero / sunxi musb fixes for the usb-proxy appliance:
#  * 0001-musb-...: the in-tree counterpart to the out-of-tree musbfix.ko kprobe.
#    Servicing a pending RX packet instead of flushing the FIFO is what makes
#    bulk-OUT reliable on single-buffered sunxi musb (ADB no longer "offline").
#    Baking it into the kernel removes musbfix's vermagic fragility entirely.
#  * 0002-usb-musb-sunxi-...: Ondrej Jirman's (megous / Armbian) patch. On H3
#    the OTG phy reroutes host traffic to EHCI/OHCI, so musb is gadget-only; it
#    forces pdata.mode=MUSB_PERIPHERAL for those SoCs and drops the bogus
#    "mode change only in dual role" check that otherwise stopped the phy from
#    switching to device — this is what makes the micro-USB enumerate as a
#    gadget (the proper fix for the OTG-stuck-in-host problem).
#  * 0003-dts-...release-phy0: the actual root-cause fix (armbian/build #8871).
#    On H3 the OTG PHY0 is dual-routed between musb and the ehci0/ohci0 host
#    pair; the board enables ehci0/ohci0, which grab PHY0 and force host mode.
#    Deleting their phys/phy-names releases PHY0 to musb so the micro-USB comes
#    up as a gadget. dr_mode stays "peripheral" (mainline default).
#  * usbproxy.cfg: build raw_gadget and the musb gadget stack into the kernel
#    (=y) so /dev/raw-gadget exists at boot with nothing to modprobe.
SRC_URI:append = " \
    file://0001-musb-gadget-service-pending-RX-packet-on-requeue.patch \
    file://0002-usb-musb-sunxi-force-peripheral.patch \
    file://0003-dts-orangepi-zero-release-phy0-from-ehci0-ohci0.patch \
    file://0004-dts-orangepi-zero-disable-wifi-ethernet.patch \
    file://usbproxy.cfg \
"
