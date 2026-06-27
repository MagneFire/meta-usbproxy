FILESEXTRAPATHS:prepend := "${THISDIR}/files:"

# Add the devmem applet (used by /usr/bin/power-tune to poke SoC registers).
SRC_URI += "file://devmem.cfg"
