# usb-proxy appliance boot script. Replaces meta-sunxi's files/arm/boot.cmd: the
# u-boot bbappend here rebuilds boot.scr from this file, and scripts/appliance.py
# flash puts it on the card's FAT partition.
#
# Differences from the meta-sunxi default, all for boot time (measured
# 2026-09-09, see DEVELOPMENT.md section 8):
#   verify=n        skip the CRC32 of the 6 MB uImage ("Verifying Checksum",
#                   ~50 ms). Header magic and size are still checked, and
#                   appliance.py flash verifies what it wrote by readback.
#   load uImage     directly: the default tries zImage and boot/zImage first
#                   (two FAT misses) and then bootz on a uImage ("Bad magic").
#   maxcpus=2       cpu2/cpu3 never come up; power-tune offlined them anyway.
#                   cpu1 stays for the active/idle hook.
#   no console=tty1 there is no tty1 on this kernel; no root=/rootwait
#                   either, the rootfs is the bundled initramfs and mmc0 is
#                   disabled in the kernel DT. (The kernel's "unable to open
#                   an initial console" line is unrelated: the cpio has no
#                   /dev/console node and BusyBox init opens it itself once
#                   devtmpfs is mounted. Harmless.)
# The kernel appends " loglevel=3" itself (CMDLINE_EXTEND). Keep ${extra} so a
# one-off "setenv extra ..." from the U-Boot prompt still works. No eMMC /
# second-SD detection: this board boots from the one SD slot only.
#
# Flash-over-USB request (scripts/appliance.py flash --usb). Linux cannot
# reach the card, so /usr/bin/usb-flash-mode writes "DFU1" into the first H3
# RTC general-purpose register (0x01f00100, survives a warm reset, zero after
# a cold power-on) and reboots. Clear it first so a failed session boots
# normally next time instead of looping. dfu_alt_info names what dfu-util may
# write: the three FAT files, and the raw SPL+U-Boot region at sector 0x10
# (1 MiB; the FAT partition starts at sector 4096). The trailing number is the
# inactivity timeout in seconds: no host within it and the boot goes on.
if itest.l *0x01f00100 == 0x44465531; then
	mw.l 0x01f00100 0
	setenv dfu_alt_info "uImage fat 0 1;sun8i-h2-plus-orangepi-zero.dtb fat 0 1;boot.scr fat 0 1;u-boot raw 0x10 0x800"
	echo "usb-proxy: flash request, waiting for dfu-util on the OTG port"
	dfu 0 mmc 0 120
fi
setenv verify n
setenv bootargs console=${console} maxcpus=2 panic=10 ${extra}
load mmc 0:1 ${fdt_addr_r} ${fdtfile}
load mmc 0:1 ${kernel_addr_r} uImage
bootm ${kernel_addr_r} - ${fdt_addr_r}
