FILESEXTRAPATHS:prepend := "${THISDIR}/files:"

# Ship our own /etc/inittab (overrides busybox's default via the prepended file
# search path). It runs usb-proxy directly under BusyBox init. The busybox
# recipe still appends a serial getty (ttyS0) line from SERIAL_CONSOLES for
# recovery access, so we don't add one here.
