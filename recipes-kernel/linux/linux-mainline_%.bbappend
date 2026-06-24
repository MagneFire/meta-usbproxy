FILESEXTRAPATHS:prepend := "${THISDIR}/files:"

# Orange Pi Zero / sunxi musb fixes for the usb-proxy appliance:
#  * 0001-musb-...: the in-tree counterpart to the out-of-tree musbfix.ko kprobe.
#    Servicing a pending RX packet instead of flushing the FIFO is what makes
#    bulk-OUT reliable on single-buffered sunxi musb (ADB no longer "offline").
#    Baking it into the kernel removes musbfix's vermagic fragility entirely.
#  * usbproxy.cfg: build raw_gadget and the musb gadget stack into the kernel
#    (=y) so /dev/raw-gadget exists at boot with nothing to modprobe.
SRC_URI:append = " \
    file://0001-musb-gadget-service-pending-RX-packet-on-requeue.patch \
    file://usbproxy.cfg \
"
