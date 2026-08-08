FILESEXTRAPATHS:prepend := "${THISDIR}/files:"

# Orange Pi Zero / sunxi musb fixes for the usb-proxy appliance:
#  * 0001-musb-...: the OUT-requeue path must never flush the RX FIFO. Two
#    fixes in one: service an already-received packet via rxstate() instead of
#    discarding it (the old ADB-"offline" bug, ex-musbfix.ko), and with no
#    packet pending don't write FLUSHFIFO at all -- issued mid-reception it
#    erratically ate one ACKed packet (delivered as a phantom ZLP), which
#    stalled every large sustained bulk-OUT (big adb push, fastboot boot) on
#    single-buffered PIO sunxi musb. See MUSB-BULK-OUT.md for the evidence.
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
#  * 0004-dts-...cap-cpu-816-add-ramoops: crash-resilience DTS bits. Deletes
#    the 1008MHz OPP so the two-state vdd-cpux GPIO regulator (1.1/1.3V) never
#    switches again (the DVFS rail transient is the prime suspect for the
#    intermittent boot-time oopses with corrupt pointers), and reserves 128KiB
#    for ramoops so crash logs survive the panic auto-reboot.
#  * 0005-dts-...disable-mmc0: the SD card is never mounted at runtime (the
#    rootfs is a kernel-bundled initramfs and the on-disk filesystems are
#    compiled out), but the controller stayed clocked and interrupting for the
#    whole uptime. Separate from 0003 because it must apply after 0004's edits
#    to the same file. U-Boot uses its own DTB, so boot is unaffected.
#  * usbproxy.cfg: build raw_gadget and the musb gadget stack into the kernel
#    (=y) so /dev/raw-gadget exists at boot with nothing to modprobe.
#  * usbproxy-resilience.cfg: panic_on_oops + 5s panic reboot + pstore/ramoops
#    (pairs with the 0004 reserved-memory node).
#  * 0006-soc-...bus-clock-policy: clock-framework-backed active/idle control
#    for AHB1/APB1 and MBUS. AHB2 (USB host) and APB2 (UART) stay unchanged.
SRC_URI:append = " \
    file://0001-musb-gadget-service-pending-RX-packet-on-requeue.patch \
    file://0002-usb-musb-sunxi-force-peripheral.patch \
    file://0003-dts-orangepi-zero-appliance-trim.patch \
    file://0004-dts-orangepi-zero-cap-cpu-816-add-ramoops.patch \
    file://0005-dts-orangepi-zero-disable-mmc0.patch \
    file://0006-soc-sunxi-add-usbproxy-bus-clock-policy.patch \
    file://usbproxy.cfg \
    file://usbproxy-trim.cfg \
    file://usbproxy-resilience.cfg \
"
