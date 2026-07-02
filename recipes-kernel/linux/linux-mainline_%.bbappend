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
#  * 0003-dts-...appliance-trim: disable the blocks this appliance never uses.
#    ehci0/ohci0 are the OTG host companion on the shared PHY0; the board
#    enables them and they grab PHY0 and force host mode (armbian/build #8871).
#    Disabling them releases PHY0 to musb so the micro-USB enumerates as a
#    gadget (dr_mode stays "peripheral") AND skips their probe/USB enumeration,
#    trimming kernel boot. Also disables mmc1 (XR819 wifi) and emac (ethernet).
#  * 0004-musb-...rx-anomaly-diagnostics: TEMPORARY rate-limited traces for the
#    sustained bulk-OUT stall investigation (OVERRUN/INCOMPRX in musb_g_rx and
#    a FIFOFULL-on-flush check in the 0001 requeue path). Silent unless the
#    anomaly fires; drop this patch once the investigation concludes.
#  * usbproxy.cfg: build raw_gadget and the musb gadget stack into the kernel
#    (=y) so /dev/raw-gadget exists at boot with nothing to modprobe.
SRC_URI:append = " \
    file://0001-musb-gadget-service-pending-RX-packet-on-requeue.patch \
    file://0002-usb-musb-sunxi-force-peripheral.patch \
    file://0003-dts-orangepi-zero-appliance-trim.patch \
    file://0004-musb-gadget-rx-anomaly-diagnostics.patch \
    file://usbproxy.cfg \
    file://usbproxy-trim.cfg \
"
