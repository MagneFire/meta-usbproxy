# Build libusb without udev. With --enable-udev (the oe-core default), libusb's
# hotplug uses libudev's monitor, which only receives events re-broadcast by the
# udev *daemon* — and this appliance has no udevd (INIT_MANAGER=mdev-busybox).
# So device disconnects never reach libusb, usb-proxy's LIBUSB_HOTPLUG_EVENT_
# DEVICE_LEFT callback never fires, and it can't exit/restart on unplug.
#
# Without udev, libusb falls back to the raw NETLINK_KOBJECT_UEVENT backend,
# which reads kernel uevents directly (no daemon needed) — so hotplug works and
# usb-proxy recovers on replug on its own (as it does on Armbian, which has udev).
PACKAGECONFIG:remove = "udev"
