FILESEXTRAPATHS:prepend := "${THISDIR}/files:"

# Add the devmem applet (used by /usr/bin/power-tune to poke SoC registers)
# and the watchdog applet (fed from inittab, see busybox-inittab bbappend).
SRC_URI += "file://devmem.cfg file://watchdog.cfg"
