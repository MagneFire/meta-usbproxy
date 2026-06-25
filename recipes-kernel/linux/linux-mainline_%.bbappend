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
#  * 0003-phy-...usb_role_switch: also megous/Armbian. The musb patch alone
#    isn't enough — the phy still reroutes the OTG port to host based on the ID
#    pin. This adds a usb_role_switch so the role can be forced; usb-proxy-run
#    writes "device" to it at boot, pinning the phy to peripheral.
#  * usbproxy.cfg: build raw_gadget and the musb gadget stack into the kernel
#    (=y) so /dev/raw-gadget exists at boot with nothing to modprobe.
SRC_URI:append = " \
    file://0001-musb-gadget-service-pending-RX-packet-on-requeue.patch \
    file://0002-usb-musb-sunxi-force-peripheral.patch \
    file://0003-phy-sun4i-usb-add-usb_role_switch.patch \
    file://usbproxy.cfg \
"
