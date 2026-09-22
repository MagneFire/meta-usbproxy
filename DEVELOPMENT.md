# Developer guide — usb-proxy appliance

Onboarding notes for the Orange Pi Zero USB-proxy appliance: how to build both
projects, the change workflows, flashing, serial access, and the quirks that cost
real time to discover. The user-facing overview is in [`README.md`](README.md);
this file is the "how do I actually work on it" companion.

> **TL;DR** — Two repos. `meta-usbproxy` (this one) is the Yocto layer that builds
> the SD image. The C++ proxy lives in a separate `usb-proxy` repo and is pulled
> into the build from the MagneFire fork's `opi` branch (SRCREV-pinned). Builds run
> in OrbStack; flashing and serial run from the Mac.

---

## 1. The two repos and how they relate

| Repo | Path (on the Mac) | Role |
|------|-------------------|------|
| `meta-usbproxy` | `/Users/darrel/Downloads/meta-usbproxy` | Yocto layer/distro — builds the appliance image. **This repo.** |
| `usb-proxy` | `/Users/darrel/Downloads/usb-proxy` | The C++ proxy source (raw-gadget + libusb). Working copy of the MagneFire fork. |

The kernel image carries the rootfs as a **bundled initramfs** (runs from RAM, no
rootfs partition — power-loss safe). The build pulls the proxy source from
`github.com/MagneFire/usb-proxy` branch `opi`, pinned by `SRCREV` in
`recipes-apps/usb-proxy/usb-proxy_git.bb`. The local `usb-proxy` repo is where you
edit; you either push to the fork and bump SRCREV, or use the fast dev loop
(§5b).

### File map (meta-usbproxy)

```
conf/layer.conf                              layer definition (scarthgap, priority 20)
conf/distro/usbproxy.conf                    the "usbproxy" distro: mdev-busybox, trimmed features
recipes-core/images/usbproxy-initramfs.bb    RAM rootfs (cpio.gz), bundled into the kernel
recipes-core/images/usbproxy-image.bb        bootable SD image (u-boot + FAT /boot, no rootfs part)
wic/usbproxy-sdcard.wks.in                   SD layout: u-boot SPL rawcopy + FAT /boot only
recipes-apps/usb-proxy/usb-proxy_git.bb       proxy recipe (cross-compile, jsoncpp compat shim)
recipes-apps/usb-proxy/files/usb-proxy-run    launcher exec'd by inittab (auto-detects UDC)
recipes-apps/usb-proxy/files/power-tune       boot-time: offline cpu2/3, kill RJ45 LEDs
recipes-apps/usb-proxy/files/config.json      proxy config baked into the image
recipes-kernel/linux/linux-mainline_%.bbappend   patches + config fragments
recipes-kernel/linux/files/0001-musb-…rx-requeue.patch  in-tree musb bulk-OUT fix
recipes-kernel/linux/files/0002-…force-peripheral.patch megous OTG peripheral-mode fix
recipes-kernel/linux/files/0003-dts-…appliance-trim.patch  DT: disable ehci0/ohci0/mmc1/emac
recipes-kernel/linux/files/0006-soc-…bus-clock-policy.patch  dynamic AHB1/APB1/MBUS policy
recipes-kernel/linux/files/0007-usb-core-…strings.patch  never fetch iConfiguration/iInterface strings
recipes-kernel/linux/files/usbproxy.cfg       raw-gadget/musb/gadget/initramfs =y + quiet cmdline
recipes-kernel/linux/files/usbproxy-trim.cfg  subsystem disables (keep NET + MODULES)
recipes-bsp/u-boot/u-boot_%.bbappend          merges the u-boot fragment
recipes-bsp/u-boot/files/usbproxy-uboot.cfg   bootdelay=0, no USB boot, DRAM 480, bootm-len
recipes-core/busybox-inittab/…bbappend        adds power-tune + usb-proxy respawn to /etc/inittab
recipes-core/busybox/busybox_%.bbappend       enables the devmem applet (used by power-tune)
recipes-support/libusb/libusb1_%.bbappend     builds libusb without udev → netlink hotplug
scripts/host-deps.sh                          install Yocto build deps (Debian/Ubuntu)
scripts/setup-build.sh                         clone layers @scarthgap + write build conf
scripts/appliance.py                          deploy + verify: swap / boot-ram / flash / check / soak (§5b, §6)
scripts/applib.py                             serial plumbing shared by the scripts (node autodetect, getty login, U-Boot, Y-modem)
scripts/uboot-flash.py, uboot-console.py      U-Boot over serial: write SPL / FAT files, RAM-boot a kernel, run a command
scripts/pi-serial.py, serial-upload.py        Linux over serial: run a command, upload a small file
scripts/boot-soak.py, power-probe.py          warm-reboot soak; wakeups/busy% probe
.claude/skills/appliance/SKILL.md             the deploy runbook (which tier, what check must print, gotchas)
```

---

## 2. Building meta-usbproxy (the SD image)

Yocto needs a Linux host. We build at native arm64 speed inside the OrbStack Debian
machine. **Keep all build state on the container's native fs (`~/yocto/...`), never
on the macOS-shared `/Users` mount** — both for speed and because kernel-yocto's
git operations misbehave on the shared mount.

```sh
# 1. Install build deps (once). Also fixes two OrbStack git defaults that break
#    kernel-yocto: core.ignorecase and commit.gpgsign (see §8).
orb run bash /Users/darrel/Downloads/meta-usbproxy/scripts/host-deps.sh

# 2. Clone layers @scarthgap and generate build/conf/{bblayers,local}.conf.
#    Re-runnable; existing clones are skipped.
orb run bash /Users/darrel/Downloads/meta-usbproxy/scripts/setup-build.sh

# 3. Build. First build is a few hours (arm64-host sstate isn't on the public
#    mirror, so native tools compile locally). Later builds are incremental.
orb run bash -lc 'export LANG=en_US.UTF-8 LC_ALL=en_US.UTF-8; \
  cd ~/yocto/usbproxy && source layers/poky/oe-init-build-env build && \
  bitbake usbproxy-image'
```

`setup-build.sh` clones poky, meta-openembedded, meta-arm, meta-sunxi (all
`scarthgap`) into `~/yocto/usbproxy/layers`, and writes `local.conf` with
`MACHINE=orange-pi-zero`, `DISTRO=usbproxy`, `DL_DIR`/`SSTATE_DIR`/`TMPDIR` under
`~/yocto/usbproxy`, `BB_NUMBER_THREADS=8`, `IMAGE_FSTYPES="wic.gz wic.bmap"`,
`INITRAMFS_IMAGE=usbproxy-initramfs`, `INITRAMFS_IMAGE_BUNDLE=1`,
`INHERIT += "rm_work"` (with `RM_WORK_EXCLUDE += "usbproxy-initramfs"`), and
`LICENSE_FLAGS_ACCEPTED="synaptics-killswitch"`.

**Output artifacts:**

```
~/yocto/usbproxy/tmp/deploy/images/orange-pi-zero/
    usbproxy-image-orange-pi-zero.rootfs.wic.gz    ← flash this
    usbproxy-image-orange-pi-zero.rootfs.wic.bmap  ← bmap for fast flash
```

To pull the image out to the Mac for flashing:

```sh
cp ~/yocto/usbproxy/tmp/deploy/images/orange-pi-zero/usbproxy-image-orange-pi-zero.rootfs.wic.gz  ~/Downloads/
# (run inside orb, or read from the shared path)
```

---

## 3. Building usb-proxy standalone (off-target, for quick compile checks)

On any Linux box (or in OrbStack) you can build the proxy by itself to check that
edits compile — no Yocto needed.

```sh
sudo apt install libusb-1.0-0-dev libjsoncpp-dev pkg-config
# optional: a Lua dev pkg (liblua5.4-dev / libluajit-5.1-dev) — auto-detected
cd usb-proxy && make          # produces ./usb-proxy
```

The upstream `Makefile` hardcodes `g++`, ignores `LDFLAGS`, and includes jsoncpp as
`<jsoncpp/json/json.h>` (Debian layout). The Yocto recipe works around all three
(see `usb-proxy_git.bb` `do_compile`); the plain `make` is only for a host smoke
test, not for producing the target binary.

### CLI flags & config

| Flag | Meaning |
|------|---------|
| `-h`, `--help` | help |
| `-v`, `--verbose` | increase verbosity (repeat for more, e.g. `-vv`) |
| `--device=<NAME>` | UDC device (default `dummy_udc.0`) |
| `--driver=<NAME>` | UDC driver (default `dummy_udc`) |
| `--vendor_id=<HEX>` / `--product_id=<HEX>` | pick a specific physical device |
| `--enable_injection` / `--injection_file=<PATH>` | MITM injection rules |
| `--enable_customized_config` | load `config.json` (used by the appliance) |
| `--auto_remap_endpoints` | remap descriptors/endpoints to UDC limits |
| `--iso_batch_size <N>` | ISO packets per transfer (1–32, default 8) |
| `--musb_out_read_packets <N>` | bulk-OUT packets per gadget read on musb (default 1; >1 needs the kernel requeue-flush fix) |
| `--persistent_gadget` | one fixed gadget for the life of the process (CDC-ACM console + adb + fastboot interfaces, usb-proxy's own identity); the proxied device's bulk endpoints are bridged onto it, the host is never re-enumerated (§7) |
| `--gadget_vendor_id <HEX>` / `--gadget_product_id <HEX>` / `--gadget_serial <STR>` | the fixed gadget's identity (default `1d6b:0104`, `USBPROXY01`) |
| `--usb_console_shell <CMD>` | what runs on the fixed gadget's console pty (default `/bin/sh -l`) |

The appliance launcher (`usb-proxy-run`) auto-detects the UDC and runs:

```sh
usb-proxy --device "$udc" --driver musb-hdrc \
          --enable_customized_config --auto_remap_endpoints
```

`config.json` (baked into the image) has these keys:

```json
{ "reset_device_before_proxy": false, "bmaxpacketsize0_must_greater_than_64": true,
  "async_bulk_out_in_flight": 16, "musb_out_read_packets": 16,
  "adb_ack_accel": true, "power_hook": "/usr/bin/power-tune", "power_idle_ms": 5000,
  "persistent_gadget": true }
```

`persistent_gadget` is the appliance's choice (it brings the USB console with
it); the usb-proxy repo's own `config.json` leaves it out, so a plain checkout
behaves like upstream and mirrors the proxied device.

`reset_device_before_proxy` is **false on purpose** — a USB reset causes
enumeration failures on this device/musb combo (see §8).

---

## 4. Change workflow — Yocto side

1. Edit a recipe / `.cfg` fragment / patch under `meta-usbproxy`.
2. Re-run the build command from §2 step 3. BitBake picks up changed files in this
   layer automatically.
3. **If a change doesn't take effect, it's almost always sstate staleness** (see
   §8). Force the affected recipe to rebuild, e.g.:
   ```sh
   bitbake -c cleansstate <recipe>        # e.g. linux-mainline, usbproxy-initramfs
   bitbake usbproxy-image
   ```
   When in doubt — especially after changing the dev manager, the initramfs
   contents, or anything that ripples into the bundled kernel — do a from-scratch
   build: `rm -rf ~/yocto/usbproxy/tmp` (keep `downloads/` and `sstate-cache/`),
   then rebuild.
4. Flash (§6) and test on hardware (§7). The appliance has no compiler or network,
   so every change is validated by reflashing the whole image.

---

## 5. Change workflow — usb-proxy side

The proxy source repo is `/Users/darrel/Downloads/usb-proxy`. Two ways to get edits
into the appliance:

### 5a. Pinned (the normal/release path)

1. Edit, commit, and push to the MagneFire fork's `opi` branch.
2. Bump `SRCREV` in `recipes-apps/usb-proxy/usb-proxy_git.bb` to the new commit.
3. Rebuild (§2). `cleansstate usb-proxy` if it doesn't pick up the new rev.

### 5b. Fast dev loop (iterating against the appliance, no fork push)

**The short form.** Point devtool at the local tree once, then every
iteration is one command that builds, uploads the stripped binary over serial
into the RAM rootfs, respawns the proxy and verifies (md5, respawn, `adb
devices`):

```sh
# once, in the build env:
devtool modify --no-extract usb-proxy /Users/darrel/Downloads/usb-proxy
# every iteration, from the Mac:
uv run scripts/appliance.py swap --adb
```

`scripts/appliance.py` is the one entry point for deploying (`swap`,
`boot-ram`, `flash`, `check`, `soak`); the project skill
`.claude/skills/appliance/SKILL.md` is the runbook for picking the tier. The
rest of this section explains what it does and the manual equivalents.

Build local working-tree edits straight into the image without pushing to the
fork. Two ways — **devtool is the recommended one.**

**devtool (recommended).** Point a Yocto workspace at the local source tree so it
builds in place — no bbappend to hand-write, no commit needed to advance the
build (it builds the working tree directly):

```sh
# In the build env (after oe-init-build-env):
devtool modify --no-extract usb-proxy /Users/darrel/Downloads/usb-proxy
# edit the source in /Users/darrel/Downloads/usb-proxy, then:
bitbake usbproxy-image          # or: devtool build usb-proxy  (recipe only)
devtool status                  # shows the active workspace recipe
devtool reset usb-proxy         # when done — restores the pinned build
```

`--no-extract <path>` makes devtool use the existing tree (an in-tree build; the
`usb-proxy`/`*.o` artifacts it drops there are already in `.gitignore`). Without
it, `devtool modify` would *extract a fresh checkout of the recipe's SRC_URI* (the
GitHub fork at the pinned SRCREV) into `build/workspace/sources/usb-proxy` — i.e.
*without* your local commits — which is usually not what you want here.

**Temporary bbappend (fallback).** If you can't use devtool, add
`recipes-apps/usb-proxy/usb-proxy_git.bbappend`:
```
FILESEXTRAPATHS:prepend := "${THISDIR}/files:"
SRC_URI = "git:///Users/darrel/Downloads/usb-proxy;protocol=file;branch=opi \
           file://config.json file://usb-proxy-run file://power-tune"
SRCREV = "${AUTOREV}"
```
Then **commit** your edit (AUTOREV builds the branch HEAD, so uncommitted changes
are invisible) and `bitbake usbproxy-image`. **Remove the bbappend when done** to
restore the pinned, reproducible build.

**Binary swap over serial (no reflash at all).** The rootfs is RAM-backed, so a
freshly built `usb-proxy` binary can be dropped onto the *running* appliance and
respawned — the whole loop (build → upload → restart → measure) is ~3 minutes
and needs no SD-card handling. Used heavily for the 2026-07-04 throughput work:

```sh
# In the build env: build, strip, gzip (the gz is ~40 KB)
devtool build usb-proxy
tmp/work/*/usb-proxy/*/recipe-sysroot-native/usr/bin/arm-poky-linux-gnueabi/arm-poky-linux-gnueabi-strip \
    /Users/darrel/Downloads/usb-proxy/usb-proxy -o /tmp/up && gzip -9f /tmp/up
# From the Mac: upload (~80 s), install, respawn
uv run scripts/serial-upload.py /tmp/up.gz /tmp/up.gz
uv run scripts/pi-serial.py "gunzip -f /tmp/up.gz && chmod +x /tmp/up \
    && mv /tmp/up /usr/bin/usb-proxy && kill -9 \$(pidof usb-proxy)" 8
```

Does not survive a reboot (RAM rootfs) — bake the change into the image when
done. See the `scripts/serial-upload.py` docstring for details/gotchas.

### Branches are controller-specific — do not cross them

- `opi` → sunxi **musb** (the Orange Pi appliance). Carries: clamp bulk/interrupt
  OUT reads to one packet on musb, the bulk-IN timeout fix, `_exit` on NO_DEVICE,
  the condvar/fast-path latency work and `adb_ack_accel` (MUSB-BULK-OUT.md §7).
- `rpi` → **dwc2** (Raspberry Pi 4), tuned differently.

`opi` will **not** enumerate on dwc2 and vice-versa, so the RPi4 can't mirror the
appliance's musb behaviour. Test musb changes on the Orange Pi.

---

## 6. Flashing the SD card (from the Mac)

**Card in the board (the normal way).** Everything the image needs on the card
is a handful of files U-Boot can write itself over the serial console, so a
release flash is:

```sh
uv run scripts/appliance.py flash --adb
```

It catches the U-Boot prompt, compares the deploy dir's uImage, DTB, boot.scr
and U-Boot against what is on the card (crc32 via `fatload`/`mmc read`), sends
only what differs (`--uboot` forces U-Boot, `--force` the FAT files), verifies
every write by readback, resets, and runs `check`: DRAM banner 256 MiB,
watchdog armed, no oops, login, `/etc/buildinfo` equal to the deploy dir's
build stamp, usb-proxy md5 equal to the build's, one real proxy running,
pstore empty, watch in `adb devices`. Transcripts go to `~/.cache/appliance/`.

**Over the OTG port instead of the UART (works since 2026-09-12):**

```sh
brew install dfu-util                              # once
uv run scripts/appliance.py flash --usb --adb
```

Measured: the 7 MB uImage writes in ~2.4 s (vs 156 s over Y-modem), and a full
`flash --usb --force --adb` round trip is ~20 s. It needs a card that already
carries the DFU-capable U-Boot and boot.scr; put those on once over the UART
with `flash --uboot`, then every later deploy can go over USB. U-Boot patch
`0002-sunxi-board-usb-init-probe-musb-gadget.patch` is what makes the musb
gadget enumerate: without it `dfu` prints "Controller uninitialized" /
`g_dnl_register -ENXIO` because nothing probes the OTG gadget controller on the
non-DM path (udc_device_get_by_index -> board_usb_init, which sunxi lacked).

Same policy (compare, write what differs, verify by readback, reset, `check`),
different transport: the board is asked to reboot into U-Boot's DFU mode and
`dfu-util` moves the files at USB speed (the 7 MB uImage in seconds, versus
136 s over Y-modem). How it gets there without a UART: Linux has no MMC
driver, so `/usr/bin/usb-flash-mode` writes the magic `DFU1` into the first
H3 RTC general-purpose register (`0x01f00100`; the RTC domain keeps it across
a warm reset and clears it on a cold power-on) and runs `reboot -f`;
`usbproxy-boot.cmd` sees the word, clears it, sets `dfu_alt_info` (the three
FAT files under their own names plus `u-boot` = raw sectors from `0x10`) and
runs `dfu 0 mmc 0 120`. The Mac then sees Allwinner `1f3a:1010`; `dfu-util
-e -R` at the end makes U-Boot reset. The `120` is an inactivity timeout: a
board that ends up in DFU with no host boots normally two minutes later, so a
stale flag cannot strand it. The request can be typed over either console
(`usb-flash-mode` at a root prompt); `appliance.py` uses whichever node
`applib.serial_node()` finds, and when that is the USB console it has no boot
log to grade, so `check` starts at the shell. U-Boot options:
`usbproxy-uboot.cfg` (gadget + `CMD_DFU`/`DFU_MMC`/`DFU_TIMEOUT`, 16 MiB file
buffer). A card whose U-Boot or boot.scr predate this must be flashed once
over the UART (`flash --uboot`); the Y-modem path stays for that and for a
board that does not come up at all.

**RAM-only test of a whole image, card untouched:**

```sh
uv run scripts/appliance.py boot-ram               # deploy dir's uImage-initramfs
```

`loady` puts the bundled uImage in RAM, `bootm` boots it with the card's DTB
and bootargs, and the same `check` runs. The next reboot is the card's own
kernel again. Use it for kernel/DT/initramfs/launcher changes before `flash`.

**Speed.** Y-modem at 115200 moves 9.0 KB/s: the 6 MB uImage took 653 s.
With `CONFIG_SYS_LOADS_BAUD_CHANGE=y` in U-Boot (`usbproxy-uboot.cfg`, on the
card since 2026-09-09) the same takes 136 s (43 KB/s): `boot-ram`/`flash`
default to 750000 (`--baud 0` forces 115200; `uboot-flash.py --baud 750000`
for the single-file form). Use 750000 and nothing else: the H3 UART
divides 24 MHz/16 by an integer, so `921600` is accepted, printed, and
actually 750000 on the wire, at which point the Mac side never syncs and the
board waits for an ENTER at a rate nobody is sending (recover by talking to
it at 750000 and `reset`). The scripts refuse rates outside the exact set and
detect a U-Boot without the option, staying at 115200.

**The kernel file is `uImage-initramfs-*.bin`** (~6 MB, rootfs inside). The
plain `uImage-*.bin` in the same directory is kernel-only; flashed as `/uImage`
it boots to `Starting kernel ...` and hangs with no init, the watchdog does not
bounce it, and only a power-cycle followed by `--catch` gets U-Boot back. The
scripts refuse a kernel file without `initramfs` in its name.

**Card out of the board (fallback: a card that no longer reaches U-Boot).**

```sh
# Identify the card first — get the disk number:
diskutil list                                   # find e.g. /dev/disk11

# Then (replace 11 with your disk number; rdisk = raw = faster):
diskutil unmountDisk /dev/disk11
sudo bmaptool copy --bmap usbproxy-image-orange-pi-zero.rootfs.wic.bmap \
                   usbproxy-image-orange-pi-zero.rootfs.wic.gz /dev/rdisk11
diskutil eject /dev/disk11
```

`bmaptool` only writes the mapped blocks, so it's fast. An occasional transient
I/O error mid-write just needs a retry. Without bmaptool:
`zcat <image>.wic.gz | sudo dd of=/dev/rdisk11 bs=4m`.

**Single files by hand** (what `appliance.py flash` does underneath). Because
the runtime rootfs is bundled in `uImage`, most software changes do not require
removing the card. Enter U-Boot, then YMODEM-write the bundled kernel and DTB
directly to the FAT boot partition:

```sh
PI_DEV=/dev/tty.usbserial-XXXX uv run scripts/uboot-flash.py --catch \
    --fat uImage /path/to/uImage-initramfs-orange-pi-zero.bin
PI_DEV=/dev/tty.usbserial-XXXX uv run scripts/uboot-flash.py \
    --fat sun8i-h2-plus-orangepi-zero.dtb /path/to/sun8i-h2-plus-orangepi-zero.dtb
PI_DEV=/dev/tty.usbserial-XXXX uv run scripts/uboot-console.py reset
```

`--catch` reboots the board and catches the `=>` prompt in the same session; without it the flasher confirms the prompt first and aborts if it is not there (so a missed catch never writes blind — the trap that briefly looked like a bad flash on 2026-09-08). The flasher CRC-checks each YMODEM transfer in RAM, writes the named FAT file,
loads it back from the card, and checks the CRC again. Use the original no-flag
form only for a U-Boot/SPL update; it writes from raw sector `0x10` instead.

---

## 7. Talking to the Orange Pi over USB serial

A USB-UART dongle on the board's debug UART, 115200 8N1. **The device node varies**
between dongles/reconnects — it has been `/dev/tty.usbserial-10` and
`/dev/tty.usbserial-11410`; check `ls /dev/tty.usbserial-*` before connecting.

Login is `root` with an **empty password** (it may prompt — just send a blank
line). Interactive:

```sh
screen /dev/tty.usbserial-10 115200      # Ctrl-A k to quit
```

Scripted: use `scripts/pi-serial.py` (in this repo — opens the port at 115200,
logs in as root with the empty password, runs a command, prints the output).
With one dongle plugged in the scripts find the node themselves
(`scripts/applib.py`); `PI_DEV` picks one when there are several. It
carries an inline uv dependency on `pyserial`, so **run it with uv** — uv builds
an ephemeral env with pyserial; there is nothing to pip-install (the system
`python3` does **not** have pyserial):

```sh
cd /Users/darrel/Downloads/meta-usbproxy
uv run scripts/pi-serial.py "<command>" [read_seconds]
uv run scripts/pi-serial.py "cat /var/volatile/log/usb-proxy.log"

# device node varies — override the default when needed:
PI_DEV=/dev/tty.usbserial-XXXX uv run scripts/pi-serial.py "uptime"
```

(The script is also marked executable with a `#!/usr/bin/env -S uv run --script`
shebang, so `./scripts/pi-serial.py "<command>"` works too. It supersedes the old
throwaway `/tmp/pi.py` / `/tmp/sercmd.py` helpers, which didn't survive a reboot.)

To drop a small file (e.g. `config.json`) onto the appliance, use `printf` and
escape the inner double-quotes for the surrounding shell — busybox in the trimmed
image has **no `base64` applet** (`base64: not found`), so decode tricks don't work:

```sh
uv run scripts/pi-serial.py "printf '%s\n' '{\"reset_device_before_proxy\": false, \"async_bulk_out_in_flight\": 0}' > /etc/usb-proxy/config.json; cat /etc/usb-proxy/config.json"
```

To restart the proxy so it re-reads the config, **`kill -9`** it (plain `kill`/SIGTERM
hits the graceful-shutdown path which can hang on the still-connected host); inittab
respawns it: `uv run scripts/pi-serial.py "kill -9 \$(pidof usb-proxy)"`.

Useful once you're in: `usb-proxy` logs to `/var/volatile/log/usb-proxy.log` (the
`usb-proxy-run` launcher) — `tail -f` it to watch the proxy. Default verbosity
prints a line per OUT packet; grep `read 512` / `read 0` to see the bulk-OUT
pattern.

### The console over the proxy port (no dongle)

Since 2026-09-12 the appliance's gadget also carries a **CDC-ACM console**: a
root shell on a pty, exposed next to the proxied device's interfaces on the
same OTG port. On the Mac it is `/dev/cu.usbmodem*`; anything that talks to a
serial port works (`screen /dev/cu.usbmodem* 115200`), and `scripts/usb-console.py`
is a small terminal that waits for the node and reconnects when it vanishes.
The `pi-serial.py` / `appliance.py` scripts use it automatically when no UART
dongle is present (`applib.serial_node()`; `PI_DEV` still overrides).

How it works (usb-proxy `console-acm.cpp`, `console-shell.cpp`): raw-gadget
owns the whole UDC, so the console cannot be a configfs/g_serial gadget beside
usb-proxy — usb-proxy adds the CDC-ACM function itself, as part of the
persistent gadget below. Its endpoints come from the tail of the UDC pool
(musb: ep4in/ep5in/ep5out) so the bridge interfaces keep ep1..; if they did
not fit, the console would give way and a log line say so. (Until 2026-09-13
the function could also be spliced into the transparent mode's mirrored
gadget, and rode a console-only idle gadget while no device was attached;
both paths were removed when the persistent gadget became the only console.)

**Persistent gadget (the appliance's mode since 2026-09-12,
`persistent_gadget: true`).** The transparent mode rebuilds the gadget from
the watch's descriptors and therefore re-enumerates on every plug, unplug and
adb↔fastboot switch, and has no console. The appliance instead presents **one
fixed gadget for the life of the process** (usb-proxy `gadget-fixed.cpp`, `bridge.cpp`): identity `1d6b:0104`,
serial `USBPROXY01`, one configuration = the CDC-ACM console (interfaces 0/1)
+ an adb interface (`ff/42/01`, iface 2) + a fastboot interface (`ff/42/03`,
iface 3). ep0 is answered locally; the watch is enumerated only on the board's
USB-A side, and its adb or fastboot bulk endpoints are **bridged** onto the
matching fixed interface while it is present (`bridge: adb slot bound` in the
log). Consequences:

- The Mac enumerates the gadget **once per proxy start** (the ioreg
  `sessionID` never changes); the console node is always
  `/dev/cu.usbmodemUSBPROXY011` and a `screen` on it survives every watch
  transition, including adb↔fastboot (verified: `uptime` every 5 s across
  `fastboot reboot` → adb, no gap).
- `adb devices` / `fastboot devices` show the **fixed serial `USBPROXY01`**,
  not the watch's. Both devices are always listed. With no watch, `adb
  devices` shows a steady `USBPROXY01 offline`: the idle adb slot **NAKs**,
  the Mac's adb opens it once and its CNXN stays pending. When the watch
  leaves the adb slot, the slot gets a 300 ms **halt pulse** so the host's
  stale read fails at once and adb drops its transport (only the host sends
  CNXN, so a transport left alive would never talk to the next adbd), then it
  is back to NAK. Until 2026-09-13 the idle adb slot
  was halted too, and macOS adb, which forgets a kicked device, re-opened it
  every second: a ~1 ms `USBPROXY01 offline` blink per second (a 1 Hz poll
  drifts into it), an interface open/close plus serial-string read per
  second, and 7 lines/s in adb's server log (84 MB in a day).
- **The pulse halts only the IN endpoint.** adb's pending read is what
  kicks it. Halting OUT too made the CNXN of the re-opened transport arrive
  without its 24-byte header, one cycle in two: musb writes CLRDATATOG on
  every set/clear halt, so the gadget's OUT toggle went back to DATA0 while
  the Mac kept its own across the re-open, and a header on the wrong toggle
  is ACKed as a duplicate and dropped. The headless banner is then dropped by
  the framer (`286 host bytes where a header was due`) and adb sits
  `offline` until its server restarts. Never touched, the OUT toggles stay
  in step (3/3 reboot cycles clean after the change, 1/3 before).
- **The fastboot slot is never halted by the bridge** (2026-09-22). Until then
  it was halted while idle and at every unbind ("`fastboot getvar` fails at
  once while the watch is in adb") and cleared at bind, which is the same
  toggle trap on both endpoints: a fastboot session normally ends with
  nothing pending, so the Mac never sees the STALL that would make it reset
  its toggles, while the gadget's went back to DATA0 twice per mode switch.
  About one session in two after `adb reboot bootloader` then lost its first
  command packet (ACKed as a duplicate, nothing logged) or its first reply
  (discarded by the Mac after a `wrote N bytes to host`) and `fastboot` hung
  until Ctrl-C. In transparent mode the Mac re-enumerated on every mode
  switch, so both sides restarted at DATA0 together. Now the idle fastboot
  slot NAKs; a command typed while the watch is still in adb parks in the
  musb RX FIFO and is delivered when the watch binds, the transparent-mode
  "waiting for device" behaviour (on macOS the halted slot never failed fast
  anyway: fastboot cleared the stall itself and then hung on the NAK). Only
  the host's own SET_FEATURE halts are cleared at bind.
- **Host data never parks in the UDC while idle.** The idle adb slot runs a
  **sink thread** (`bridge.cpp` `sink_main`) that reads its bulk OUT into the
  stream framer: the CNXN is recorded in user space (log: `idle adb slot:
  host CNXN captured`) and replayed the instant a device binds (`replaying
  the host's last CNXN`); anything else is dropped. Without it the CNXN sat
  in the musb RX FIFO for the whole watch reboot, through `power-tune idle`'s
  bus-clock drop; the first headless-banner loss was seen in exactly that
  state, and whether the FIFO or the toggle above lost it was not settled.
  Clearing a halt on musb also flushes that FIFO, which is why
  `set_halt_locked()` never clears a slot that is not halted.
- A device without an adb/fastboot interface is **ignored** while it is on
  the bus (log: `does not fit the adb/fastboot template`). The Moto 360
  presents such a device — `18d1:0afe`, one mass-storage interface
  `08/06/50` — in some of its modes; the gadget stays up and the manager binds
  the adb instance that follows. The transparent mirror is only used when the
  fixed gadget cannot attach at all.
- Only the watch's adb/fastboot/TWRP-adb bulk traffic is carried; MTP, audio
  and any control traffic of the proxied device are not (they are not needed
  for adb/fastboot, which are pure bulk after enumeration).
- The proxy no longer exits when the watch leaves; it exits only on a fatal
  gadget error (then inittab respawns it, the one re-enumeration left).

What differs from the UART, by design:

- Output is **dropped while the Mac has the port closed** (DTR low), so a
  closed port never stalls the shell. Open the port first, then look.
- **U-Boot is not behind it.** `boot-ram`, `flash` (serial) and `--catch`
  need the UART; `flash --usb` is the console-only deploy route.
- With `persistent_gadget: false` (transparent mode) there is **no USB
  console at all**: the Mac sees the byte-exact mirror of the proxied device,
  and only the UART is left for a shell.

### The fastboot test topology

```
Mac (fastboot host)  ──►  Orange Pi micro-USB (gadget/UDC, musb)
                          Orange Pi USB-A host port  ──►  target device (e.g. watch)
```

Run `fastboot` / `adb` **on the Mac** — the proxy forwards Mac→device. The Orange
Pi is also typically **powered over that same micro-USB** from the Mac (relevant to
the DRAM-droop issue in §8).

---

## 8. Quirks & gotchas (the time-savers)

**Build / Yocto**

- **sstate staleness is brutal here.** A config change that ripples into
  `packagegroup-core-boot` → the initramfs → the bundled kernel → the wic will
  serve stale artifacts at each layer. `cleansstate` the specific recipe, or do a
  from-scratch `rm -rf tmp` (§4). After `cleansstate` you may see
  `do_package basehash changed … not deterministic` — just re-run bitbake.
- **`WKS_FILES` (plural), not `WKS_FILE`.** meta-sunxi's `sunxi.inc` sets
  `WKS_FILES`, which the wic class resolves ahead of the singular `WKS_FILE`.
  Setting only `WKS_FILE` is silently ignored and you get the stock
  boot+ext4-rootfs layout (a dead ~80 MB rootfs partition). See the comment in
  `usbproxy-image.bb`.
- **`VIRTUAL-RUNTIME_dev_manager = "busybox-mdev"` must be a hard assign.**
  `packagegroup-core-boot` sets it `?= "udev"`, which beats INIT_MANAGER's
  `??= "busybox-mdev"`, dragging in eudev → kmod → libcrypto (OpenSSL, ~3 MB).
  The hard assignment in `usbproxy.conf` is what keeps the image small.
- **`IMAGE_NAME_SUFFIX = ""` on the initramfs recipe.** `do_bundle_initramfs`
  looks for `${INITRAMFS_IMAGE}-${MACHINE}.cpio.gz`, but scarthgap deploys
  `*.rootfs.cpio.gz` by default — without the empty suffix the bundle step can't
  find the cpio.

**Kernel**

- **`CONFIG_CMDLINE_EXTEND=y` needs a non-empty `CONFIG_CMDLINE`.** It's a Kconfig
  *choice* defaulting to `FROM_BOOTLOADER`; EXTEND only wins the choice when
  `CONFIG_CMDLINE` is also set in the same fragment. EXTEND alone silently reverts
  and the boot goes verbose. Both lines are in `usbproxy.cfg`.
- **Keep `CONFIG_NET=y`.** libusb is built `--disable-udev`, so it opens a
  `NETLINK_KOBJECT_UEVENT` socket for hotplug; `CONFIG_NET=n` makes `libusb_init()`
  fail with `LIBUSB_ERROR_OTHER` and **the gadget never attaches**. Strip the NIC
  drivers (`NETDEVICES`, `INET`, wifi/BT) but keep the NET core. See
  `usbproxy-trim.cfg`.
- **Do not set `CONFIG_MODULES=n`.** The defconfig marks ~630 drivers `=m`; with
  modules off, oldconfig *promotes* the still-enabled ones to `=y` (into vmlinux),
  making the kernel **bigger and slower to build**. Keep `MODULES=y` and disable
  whole subsystems instead.
- **Don't trim deeper than the current 42 s / ~3.7 M floor.** Disabling
  `MACH_SUN7I` removes the only `select HAVE_ARM_ARCH_TIMER` → H3 loses the arch
  timer (100 Hz dummy timer, 1 CPU, ~10 s stalls). A deeper trim also hit a
  regulator deferred-probe stall that left the USB PHY on dummy supplies so the
  gadget never attached. Reverted; not worth ~5 s / 0.5 MB.

**U-Boot**

- **`CONFIG_SYS_BOOTM_LEN=0x4000000`.** The bundled uImage exceeds u-boot's default
  ~8 MB bootm load limit (it was ~10 MB pre-trim → `uncompress error -28`). Keep
  the headroom even though the trimmed image is now ~6 MB.
- **`CONFIG_DRAM_CLK=312`** (down from the 624 default via 480). Originally
  taken against intermittent boot-time kernel oopses with corrupt pointers (e.g.
  a page-clear faulting at a garbage address), read at the time as marginal-DRAM
  / voltage-droop corruption. The 2026-09-08 finding (§9, "DRAM size
  misdetection") is a better fit for those oopses; 312 stays because it costs
  nothing and adds margin. Best paired with a solid 5 V supply.
- **The SPL banner must say `DRAM: 256 MiB`.** `512 MiB` on this board is a
  misdetection, fixed by U-Boot patch
  `0001-sunxi-dw-dram-use-pattern-based-size-detection.patch`. See §9.

**USB / runtime**

- **The three kernel patches are what make the gadget work**: `0001` (musb
  RX-requeue, replaces the old out-of-tree `musbfix` kprobe), `0002` (megous
  force-peripheral), `0003` (DT trim deleting `phys`/`phy-names` from
  `ehci0`/`ohci0` so PHY0 is released to musb — armbian/build issue #8871). Without
  the DT change the UDC stays `not attached` and the phy logs `Changing dr_mode`.
- **`reset_device_before_proxy: false`.** A USB reset triggers enumeration failures
  on this device, which is why the custom `config.json` disables it.
- **No supervisor in `usb-proxy-run` — it's a plain `exec`.** usb-proxy `_exit`s on
  `LIBUSB_ERROR_NO_DEVICE` (disconnect) and inittab respawns it. An earlier
  background-supervisor version orphaned instances and wedged the UDC with
  "couldn't find an available UDC or it's busy".
- **"Needs a replug after `adb reboot bootloader`" — FIXED (2026-09-08,
  usb-proxy `f34e120`).** The `_exit` above only ever ran from an endpoint
  thread. If the device dropped off the Pi's host port while only ep0 traffic
  was in flight (Mac still enumerating, or right around SET_CONFIGURATION,
  before any bulk thread existed) the proxy stalled ep0 forever on a dead
  handle with the gadget still attached: the Mac saw a device answering
  nothing, inittab never respawned, and the fastboot device that appeared next
  was never proxied — only a power cycle helped. The hotplug callback was no
  net either (`kill(0, SIGINT)` only sets flags; the ep0 ioctl is not
  interrupted, and `main()` then joins the endless hotplug thread). The watch
  makes that window easy to hit: a cradle attach enumerates three times
  (devices living 2.2 s, 0.33 s, then the real one) and every reboot
  double-enumerates. Fix, three layers in usb-proxy: NO_DEVICE from the ep0
  path exits; the hotplug callback exits directly; and the hotplug/event
  thread checks the device's devtmpfs node `/dev/bus/usb/BBB/DDD` once a
  second (removed by the kernel the instant the device leaves, no bus traffic)
  — logs `Device node ... gone, exiting usb-proxy`, and in practice fires on
  every transition. Debounce: a newly appeared device must survive 1 s before
  the gadget attaches (usb-proxy `--settle_ms`, checked against the devtmpfs
  node; the launcher passes it for every instance after the first since boot,
  `/run/usb-proxy.started`), and the gadget stays detached ≥1 s between
  instances (`/run/usb-proxy.detached` stamp, launcher), so the Mac always sees
  one clean disconnect/connect per identity (the ms-scale re-attach was the
  recorded macOS stale-object trigger, `MUSB-BULK-OUT.md` §7b). Boot with a
  device already attached attaches immediately.
  If a hang still happens, run `uv run scripts/mac-usb-unwedge.py` on the Mac
  *before* replugging: it clears the macOS wedge without power-cycling the Pi,
  so `/var/volatile/log/usb-proxy.log` survives for a post-mortem.
- **usb-proxy waits for the device itself; the launcher no longer polls
  (2026-09-08).** Why adb/fastboot took seconds longer to appear through the
  appliance than direct: not the datapath. Measured with appliance `dmesg`
  against the Mac's adb server log (`$TMPDIR/adb.501.log`, "reported max packet
  size" = transport up), same watch, same Mac: with the proxy already waiting
  (cold boot) adb was up **1.2 s** after the kernel's `new high-speed USB
  device` line — that 1.2 s is everything the proxy, macOS enumeration and
  adb's own 1 s device poll cost together. After a reconnect it was **3.8 s**,
  because the old launcher found no device, did `sleep 2; exit 0`, and inittab
  respawned it — ~2.9 s per cycle (7 cycles in 20.5 s) — so a returning device
  sat enumerated for 0–3 s before the proxy was even launched, plus the 1 s
  settle. Two transitions per adb→bootloader→adb round trip made that 5–6 s.
  Now `connect_device()` polls the libusb device list every 100 ms (sysfs only:
  ten full scans took 46 ms at 648 MHz), does the settle in-process, and
  `main()` retries a failed attempt after 200 ms instead of 1 s; `libusb_init`
  runs once (each retry used to leak a context). The launcher is a plain
  `exec` after the UDC check. Power with nothing attached is handled by
  usb-proxy's own policy (it starts wound up, winds down after `power_idle_ms`,
  and winds up again the moment a device is opened), which replaces the old
  launcher's `power-tune idle` branch. Measured after the change: adb up
  1.85 s after the kernel's enumeration line on a reconnect (was 3.8 s), the
  Mac listing the gadget within 50 ms of the proxy attaching. usb-proxy now
  stamps its milestone log lines with `[uptime]` (CLOCK_MONOTONIC, the same
  clock as `dmesg`) so this can be re-measured from the log alone; with
  `--verbose` it also stamps each step of `connect_device()`.
- **The minnow fastboot bootloader cost a further 5 s: a kernel string read
  the bootloader never answers (2026-09-08).** With the launcher fixed, the
  bootloader still took 5.0 s from "device found" to "opened" (Android: 1.0 s,
  the settle). All of it was inside `libusb_open()`: usbfs open takes the
  device lock, and the kernel was holding it in `usb_set_configuration()`
  reading the configuration string (the bootloader's config descriptor has
  `iConfiguration=4`; sysfs `configuration` shows `N/A`, i.e. the read failed
  after the 5 s `USB_CTRL_GET_TIMEOUT`). The Mac never asks for that string,
  which is why a direct connection never paid it. Fix: kernel patch `0007`
  sets `USB_QUIRK_CONFIG_INTF_STRINGS` on every device in
  `usb_detect_quirks()`, so usbcore never fetches iConfiguration/iInterface
  strings (they only fill sysfs `configuration`/`interface`, which nothing
  here reads; the Mac's own string requests are forwarded live regardless).
  It sits before the dynamic-quirk XOR, so `usbcore.quirks=VID:PID:d`
  (runtime: `/sys/module/usbcore/parameters/quirks`) switches it back off
  for one device. The first fix was that runtime parameter written by the
  launcher for `22b8:42d1` only, because the parameter has no wildcard.
  Verified: bootloader found→opened 1.0 s,
  `adb reboot bootloader` → `fastboot devices` 9.3 s instead of 13.3 s
  (the rest is the watch's own reboot). Diagnostic pattern worth keeping:
  sysfs strings/`descriptors` of the device while in the slow state show
  which string index the device cannot serve.
- **Plug-in → adb budget, and what was trimmed (2026-09-09).** Measured on
  the rig from the serial timestamps of a cold boot against the Mac's adb
  server log, cross-checked with the kernel-clock `[uptime]` stamps in the
  proxy log and `/proc/<pid>/stat` start ticks. From the SPL banner to adb's
  "reported max packet size": **3.26 s**, made of: SPL 0.30; U-Boot proper
  0.57 (0.26 fixed, 0.26 reading the 6 MB uImage at 22 MB/s, 0.05 CRC);
  zImage self-gunzip 0.35; kernel to `/init` 0.33; `/init` to the usb-proxy
  fork 0.51 (the whole inittab sysinit chain plus power-tune ran first);
  usb-proxy to the Mac's SET_CONFIGURATION 0.30; and then **0.90 s inside
  macOS/adb** — adb's macOS backend (`client/usb_osx.cpp`, `RunLoopThread`)
  rescans IOKit with a fixed `sleep_for(1s)`, so 0–1 s of jitter is adb's
  own and cannot be fixed from the appliance. The gap from physical plug-in
  to the SPL banner (BROM, SD detect, VBUS) is not in this number. Trimmed:
  (1) `/etc/inittab` is now this layer's own file — usb-proxy forks right
  after proc/sysfs/devtmpfs are mounted, with power-tune and the pstore
  mount as `once` entries alongside it (BusyBox init runs every `sysinit`
  entry to completion before forking any `once`/`respawn`); (2) the zImage
  is LZ4- instead of gzip-compressed (`CONFIG_KERNEL_LZ4`); (3) our own
  `boot.scr` (`recipes-bsp/u-boot/files/usbproxy-boot.cmd`): `verify=n`,
  load `uImage` directly, `maxcpus=2`, no `console=tty1`/`root=`. Measured
  after (three warm reboots, SPL banner → adb transport): **2.61 / 2.56 /
  2.88 s** (was 3.26); "Starting kernel" → getty 0.63 s (was 1.19); usb-proxy
  forks at t=0.41 (was 0.84), `device found` t=0.77, Mac SET_CONFIGURATION
  t=1.01 (was 1.13). The uImage grew 6.03 → 6.99 MB (LZ4). What is left is
  bounded by the kernel's own enumeration of the watch (`new high-speed USB
  device` at t≈0.55, usable ~0.2 s later), U-Boot's SD reads, and adb's
  poll. Re-measure with a stamped serial capture of `reboot` (the scratch
  script was a 30-line pyserial loop stamping `U-Boot SPL`, `Starting
  kernel`, `starting pid`) against `$TMPDIR/adb.501.log`.
- **RJ45 LEDs**: off via `H3_EPHY_LED_POL` (bit17) in syscon `0x01c00030`
  (`power-tune` writes `0x78000`). The PHY is already gated/in-reset at boot; only
  the LED polarity bit needed flipping. The clock-gate/reset/shutdown/MDIO routes
  were dead ends.

**Large sustained bulk-OUT (`fastboot boot` / big `adb push`) — FIXED
(2026-07-02).** These used to stall erratically (fastboot ~94%, adb push at
10–100 KB). Root cause: stock `musb_ep_restart()` wrote FLUSHFIFO on every OUT
requeue; racing a packet in mid-reception it erratically destroyed one ACKed
packet (delivered as a phantom ZLP), deadlocking length-framed streams. Kernel
patch `0001` (v2) removes the flush entirely. Verified: repeated 10–50 MB
pushes complete md5-exact at ~1.2 MB/s. **See
[`MUSB-BULK-OUT.md`](MUSB-BULK-OUT.md)** for the investigation record, the
evidence, and the diagnostic tooling (`adb_bulk_diag`, since removed from
usb-proxy) that found it.

---

## 9. Crash resilience (reboot-on-panic, watchdog, ramoops)

**Background (2026-07-03).** The appliance intermittently oopsed, mostly during
boot, with random pointer corruption — e.g. an oops in plain `memset` where the
destination register held odd-aligned garbage while every other register was
correct. That signature (register/stack corruption in trivial code paths,
random victim process) is hardware-level marginality, not a kernel code bug.
It's the same symptom that prompted the DRAM 624→480 MHz downclock in
`recipes-bsp/u-boot/files/usbproxy-uboot.cfg`, which reduced but didn't
eliminate it. Live measurement then showed the prime remaining suspect:
`vdd-cpux` is a two-state GPIO regulator (1.1 V/1.3 V on PL6), only the
1008 MHz OPP needs 1.3 V, and schedutil flapped 648↔1008 many times per second
under mere shell activity — toggling the rail (4 ms RC ramp) with samples
catching `1008 MHz @ 1.1 V` interleavings. Boot = peak flapping = the crash
window.

Three layers now handle this:

| Layer | What it covers | Where |
|---|---|---|
| CPU capped at 816 MHz (1008 OPP deleted) | Removes all rail switching — both remaining OPPs run at one voltage. 1.1 V until 2026-09-09; since then 1.3 V, the rail's power-on state (experiment, patch `0008`, see the decode below). No perf cost: the proxy is USB-RTT-bound and idles at 648 MHz. | kernel patches `0004`, `0008` |
| `panic_on_oops` + panic timeout | Any oops/panic prints in full, then reboots ~10 s later instead of limping on with corrupt state (oops) or hanging forever (panic). (The Kconfig sets 5 s but meta-sunxi's `boot.scr` passes `panic=10` on the cmdline, which wins — fine.) | `usbproxy-resilience.cfg` |
| Hardware watchdog, armed from U-Boot | Silent hard hangs anywhere from U-Boot through userspace → hardware reset in ≤8 s. U-Boot arms+feeds it (`CONFIG_WDT` + `CONFIG_WATCHDOG_AUTOSTART` — the latter is default-**off** on sunxi upstream and without it the boot window is unarmed), busybox `watchdog -T 8 -t 2` takes over from inittab. Boot banner must say `WDT: Started watchdog@1c20ca0 ... (8s timeout)`, not `WDT: Not starting`. | `usbproxy-uboot.cfg`, busybox `watchdog.cfg`, inittab bbappend |

**Post-mortem: ramoops/pstore.** The rootfs is RAM, so without persistence a
self-reboot would erase all evidence. Patch `0004` reserves 128 KiB at
`0x4fc00000` (~4 MB below the 256 MB top-of-RAM, clear of U-Boot's relocation)
and `usbproxy-resilience.cfg` enables `PSTORE_RAM` + `PSTORE_CONSOLE`. After
any crash-reboot, read the previous kernel's oops and console log:

```sh
ls -la /sys/fs/pstore/            # mounted from inittab at boot
cat /sys/fs/pstore/dmesg-ramoops-0    # the oops/panic dump
cat /sys/fs/pstore/console-ramoops-0  # trailing console log
rm /sys/fs/pstore/*                   # ack/clear after reading
```

A `console-ramoops-0` from the previous boot is normal (`PSTORE_CONSOLE` writes
it every boot); a crash leaves a `dmesg-ramoops-*` record.

Drills: `echo c > /proc/sysrq-trigger` forces a panic (tests the reboot + the
pstore record); `kill -STOP $(pidof watchdog)` starves the watchdog (tests the
≤8 s hardware reset).

**Verified on hardware 2026-07-03**: settings all active (816 cap, rail pinned
at 1.1 V); panic drill → full print → auto-reboot → `Panic#1` readable in
pstore; watchdog starvation → reset within the 8 s budget; U-Boot banner shows
the WDT armed at power-on; 10/10 warm-reboot soak with zero oopses (boot was
the historical crash window); 10 MB `adb push` 2.6–2.7 MB/s md5-exact (no
regression from the CPU cap).

**If corruption recurs at 816 MHz**: check the SPL `DRAM:` banner first (see
below), then pstore for the signature. DRAM is at 312 already; after the size
check, suspect the 5 V supply path (powered from a host USB port = voltage
droop — use a solid supply).

**Root cause found for one class of these (2026-09-08): the SPL misdetects the
DRAM size.** (Not all of them; see the soak note at the end of this block.) Boot log: SPL banner `DRAM: 512 MiB` on this 256 MiB board, then
at 0.34 s a NULL deref at 0x80 in `free_unref_page_prepare` from
`free_reserved_area` / `kernel_init`, i.e. while freeing `.init`. Decoded
against the build's `vmlinux`/`System.map`: `r4` is the `struct page`
(`0xdfc10000` = phys `0x5fc10000`), `r8` the pfn (`0x40b00` = `c0b00000`, an
`.init` page), and `page->flags >> 30` selected the empty `ZONE_HIGHMEM`, whose
`pageblock_flags` is NULL. `0x5fc10000` minus 256 MiB is `0x4fc10000`: the
ramoops **console zone** (base `0x4fc00000` + two 32 KiB dump zones). The
ramoops header signature `0x43474244` ("DBGC") has bits 31:30 = 01 = zone 1.
So the upper 256 MiB was a mirror of the lower, the kernel (told 512 MiB by
U-Boot) put `mem_map` at the top of the phantom half, and ramoops wrote its
header through the mirror onto a `struct page`.

Why U-Boot says 512: `arch/arm/mach-sunxi/dram_sunxi_dw.c` sizes DRAM with
aliasing tests, one write pair per row/bank/page step, as the very first
accesses after the controller is reconfigured. Miss the alias once at
`row_bits = 14` and the loop settles on 15 = 512 MiB. U-Boot proper does not
re-measure (it reads the SPL header) and writes 512 MiB into the DT memory
node. Upstream saw the identical "detected double the size" flake on H616 and
fixed it with a 16-word pattern test (v2025.07, `38080293867`), but only wired
it into the H6/H616 helper; the fix here is a port of that to the H3 driver
(`recipes-bsp/u-boot/files/0001-sunxi-dw-dram-use-pattern-based-size-detection.patch`).
Armbian carries the same upstream patch and an H6-only settle delay; it has
nothing extra for H3.

Consequences worth knowing:

- **Any boot whose banner says 512 MiB can never keep a pstore record**:
  `memmap_init` writes `mem_map` through the mirror over the whole ramoops
  region before ramoops probes, so "empty pstore after a crash" is not evidence
  of a watchdog reset on such boots.
- A flake in the bank/page-size loops aliases *inside* the lower 256 MiB and
  corrupts U-Boot proper itself — the 2026-08-06 "SPL printed 512 MiB and U-Boot
  proper never ran" hang, previously blamed on a corrupt flash.
- With older kernel layouts the console header/text landed on `struct page`s of
  free pages just above the kernel instead of an `.init` page: random free-list
  corruption, surfacing as the "clear_page at a garbage address" oopses. That
  is inferred, not observed, and the soak below says it is **not the whole
  story**.
- **Soak with the patched SPL (2026-09-08, warm reboots over serial):** 23/23
  boots printed `DRAM: 256 MiB` and pstore records now survive reboots. But
  boot 2 still oopsed at 0.73 s: `power-tune` (pid 78) in `mt_find` from
  `find_vma` during its own `execve`, node pointer `0x0004448d`, i.e. a
  corrupted maple-tree node in a freshly created mm. That is the classic
  random-pointer signature on a boot whose DRAM size was right, so the
  misdetection explains the `free_initmem` crash and the pstore losses, not
  every corruption. Decoded below.
- **First thing to check in any boot log: the `DRAM:` line.** If it is not
  256 MiB, nothing after it is meaningful.
- If a 512 banner ever appears with the patch in place, the next step is a hard
  expected-size guard in the SPL (retry the DRAM init, then reset), not another
  clock reduction.

**The residual oops, decoded (2026-09-09).** The record from soak boot 2 was
read against the build's `vmlinux` (objdump in the OrbStack tree). The call
chain is `get_arg_page` → `get_user_pages_remote` → `__get_user_pages` →
`gup_vma_lookup` → `find_vma` → `mt_find`:

- `get_user_pages_remote` keeps `mm` in `r8`, takes the mmap lock at `r8+0x7c`
  (that worked, so `mm` was right), then passes it as `r0`.
- `__get_user_pages` does `mov ip, r0` … `str ip, [sp, #16]` and later reloads
  `ldr r0, [sp, #16]` for `gup_vma_lookup`; `find_vma` adds `0x40` (`mm_mt`),
  `mt_find` derefs `+8`. Fault address `0x44495` = `0x4448d + 8`,
  `0x4448d - 0x40 = 0x4444d`, and the stack dump shows `0004444d` in exactly
  that spill slot (`d0a31e60`).
- At the oops **`r8` still held the correct `mm_struct`** (`c12982c0`, which
  the kernel itself identified as a live slab object), and every neighbouring
  word in the spill slot's cache line (`current`, flags, the canary, `pages`,
  `locked`) was intact.

One spilled register came back as a different whole word, within a dozen
instructions and with its source register untouched. A DRAM cell fault flips
bits; a row/alias fault takes out a burst, not one word. This is on the CPU
side: register file, store/forwarding path, L1/L2, or the IRQ save/restore
path if an interrupt landed in that window. The older "memset with an odd
destination register" and "clear_page at a garbage address" oopses are the
same family (an address register going bad inside a store-only loop). The
value `0x4444d` is most likely a stale pfn-sized word; it also falls inside
U-Boot's PSCI monitor (`0x44000`–`0x47c00`), but nothing enters the monitor at
0.73 s (no SMC, no hotplug), so that is a coincidence.

**What the board is, per the v1.1 schematic and this unit's repairs (user,
2026-09-08).** Read this before reasoning about supplies:

- `vdd-cpux` is an SY8113B buck from DCIN-5V; its VSET (PL6, Q5) is pulled up
  to VCC-RTC, so the **power-on state is 1.3 V** (the DT's `gpios-states = <1>`
  is right). U-Boot runs the core at 1008 MHz there; the kernel drops the rail
  to 1.1 V once cpufreq settles on 816 MHz.
- **The board is powered at the 26-pin header's 5 V pin (DCIN-5V), not the
  micro-USB.** The micro-USB VBUS goes through Q10 (AO3415A) with a BCM856BS
  ideal-diode controller, so header power cannot backfeed the Mac's VBUS,
  and the OTG connector carries data only. That retires the connector-droop
  theory below as a corruption cause. The USB-A host port's VBUS is DCIN-5V
  too.
- **Repair on this PCB: U5 (the SY8008B AVCC buck) is dead. AVCC is fed from
  U6 (PST73133BETV, a 300 mA LDO from DCIN-5V, the former WiFi supply), and
  the WiFi IC is removed.** AVCC also feeds VCC3V-PLL (R25) and VCC-RTC (R46):
  the CPU/DDR PLL supply and the PL-domain pull-ups now sit behind a small LDO
  and a bodge. A PLL supply glitch would produce exactly the core-side
  signature above.
- VCC-DRAM 1.5 V is an SY8008B from VCC-5V (2.5–5.5 V input, tolerant of
  droop). The SY8113B needs ≥ 4.5 V in, so a 5 V droop reaches the core rail
  only if the board-side 5 V falls under ~4.5 V.
- DRAM 312 MHz is tCK 3.2 ns; DDR3 with the DLL on is specified to 3.3 ns.
  312 is the floor, not a step on a ladder.

**Suspects, re-ranked:** (1) core rail / PLL margin during boot: cpufreq's
648↔816 PLL relocks are densest at boot, the rail switches 1.3→1.1 V at
cpufreq init, and the PLL supply is on a repaired path; (2) the header 5 V
supply and its leads, only through regulator headroom; (3) DRAM cells, least
likely for this signature.

**Experiment ladder (2026-09-09):**

1. Patch `0008` holds `vdd-cpux` at 1.3 V at both OPPs: no rail switch per
   boot, the core at the margin of the stock top state. Deploy with
   `scripts/appliance.py flash --adb`, then `appliance.py soak 100`. At the
   observed ~1/23 rate, 100 clean boots means the rate dropped (99 %).
   **Result 2026-09-09: 100/100 warm boots, all 256 MiB, no oops, pstore
   empty** (`~/.cache/appliance/soak-20260909-190600.log`); the regulator
   read 1300000 µV and did not move across `power-tune idle|active`; `mtest
   0x40000000 0x4a000000 0 1` from the U-Boot prompt: 0 errors; `check
   --reboot --adb` all PASS; a 50 MB `adb push` through the proxy was
   md5-exact in 9.7 s. That is the first run of that length without an
   oops on this board. Kept. It does not say *which* of the two things it
   removed mattered (the 1.3→1.1 V step at cpufreq init, or core margin at
   1.1 V), only that the rail was in the loop; step 2 would tell, if it is
   ever worth knowing. The cold-power soak (`soak 20 --cold`) is still owed.
2. If it still oopses: `CONFIG_CPU_FREQ_DEFAULT_GOV_PERFORMANCE=y` with 1.1 V
   restored removes the boot-window relocks and separates "transitions" from
   "voltage".
3. If both still oops, the AVCC/PLL repair needs a scope (AVCC, VCC3V-PLL,
   VDD-CPUX and the header 5 V pin during the 0.3–1 s boot window), or the
   same image on an unrepaired Orange Pi Zero for 100 boots.
4. DRAM, to exclude rather than chase: `mtest 0x40000000 0x4a000000 0 20` from
   a cold U-Boot prompt (`CONFIG_CMD_MEMTEST`, `uboot-console.py --catch`)
   and `memtester 64M 20` on the running appliance, both in the image now.
   Cold power-ons: `appliance.py soak 20 --cold` (it prompts for each
   power-cycle and saves any pstore record next to its log).

**Earlier theory (2026-08-06, user's, unproven): a flaky micro-USB OTG
connector.** Retired for the corruption on 2026-09-09 (the board is powered
at the header, see above); kept because it may still explain the stale
enumerations and wedged Mac USB ports. It explained what the clock changes
could not:

- The corruption *persisted* through 624→480, through the DVFS rail fix, and
  through 480→408. A cause that was never DRAM would behave exactly like that.
- It was worst at boot — which is peak current draw, so peak droop across a
  marginal contact.
- The same connector carries **both VBUS and the gadget data path to the Mac**.
  So it also explains the recurring stale enumerations, wedged Mac USB ports and
  "adb can't see the device" episodes, which had been written off separately as
  macOS USB-stack flakiness. One cause for two symptom families beats two.

If this is right, the DRAM ladder and possibly the 816 MHz cap were treating a
symptom of a bad connector. That does not make them harmful — DRAM 312 costs no
throughput and saves ~20 mW — but it does mean **do not reach for another clock
reduction next time**. Test the connector first:

- Watch the meter's *voltage* during boot (peak current). Droop or a dip there is
  the signal; a steady 5.14–5.15 V is not.
- Swap the cable. Cheapest possible experiment, and cables are the usual culprit.
- Best fix if the board's power path allows it: feed 5 V from a solid supply and
  leave the OTG connector carrying data only, so a marginal contact can no longer
  brown out the SoC. **Check for backfeed first** — the Mac still presents VBUS on
  that port, and two 5 V sources meeting is its own problem.

## 10. Idle power

Measured 2026-08-06 with a USB wattmeter on the appliance's 5 V feed, and with
`scripts/power-probe.py` for the cheap A/B metric (wakeups/s and CPU-busy%
sampled over the serial console — no meter needed, and it tracks the meter well).

| State | Meter |
| --- | --- |
| Appliance alone, nothing attached | 0.40 W |
| Appliance + watch enumerated, nothing talking to it | 0.64 W |
| Appliance + watch + proxy idle — before this work | 0.74 W |
| Appliance + watch + proxy idle — after | **0.70 W** |
| Watch alone, straight to the host, idling | 0.30 W |

Flashing the image with mmc0 disabled did not move the meter — see the decoding
warning below.

**Where it went.** The one real software finding was in usb-proxy: `ep_loop_write`
polled its queue with a bare `wait_for(1ms)`, so each endpoint burned 1000
wakeups/s at complete idle. With two bulk endpoints that measured **1962
wakeups/s and 3.34 % CPU busy** on a load-average-0.00 board. Making the wait
predicated dropped it to **28 wakeups/s and 0.25 %**. On top of that,
`power-tune active|idle` (driven by usb-proxy's `power_hook`) takes the board to
one core at 648 MHz while nothing is happening, and `power-tune boot` now gates
the Display Engine that U-Boot leaves clocked.

**Read the size of the win before doing more.** All of that together — removing
essentially all CPU activity, offlining a core, cutting the clock 20 %, gating a
clock domain — moved **35 mW**. The CPU/clock domain is simply not where this
board's idle power goes; do not spend more effort there.

**The USB link cost is not the appliance's to save.** Stopping the proxy with the
watch still enumerated drops the meter to 0.64 W, so keeping the adb link alive
costs ~65 mW. But the watch attached-and-unpolled draws 0.24 W (0.64 − 0.40), and
0.24 + 0.065 = 0.305 W — which is exactly what the watch draws plugged straight
into the host. That 65 mW is the watch's own cost of a live USB link and appears
with no appliance in the path. Polling less would only trade latency for someone
else's power.

**Measure this rig properly or not at all.** A USB wattmeter here flickers over a
~40 mW band (0.30–0.34 W) with the supply itself moving 5.14–5.15 V, and with the
watch attached there is another 0.24–0.30 W of *variable* load drifting with the
watch's own state. Anything smaller than ~40 mW is invisible to a single
instantaneous reading. A whole DRAM ladder was "measured" that way and produced
confident numbers (30 mW for 480→408, 15 mW for 408→360) that were pure drift —
the tell was 312 reading *higher* than 360, which is physically backwards, and
reflashing 360 then reproduced the same high number. The method that works:

1. Unplug the proxied device — only the SBC on the meter.
2. Stub `usb-proxy-run` so the respawn loop is gone, and `power-tune idle` to pin
   one core at 648 MHz.
3. Compare a **large** delta back-to-back in one sitting, and re-measure the
   first setting at the end to prove the difference exceeds drift.

**Restoring that stub has a trap.** If the stub is `exec sleep 999999`, the exec
replaces the shell, so the process inittab is tracking *is* `sleep` — putting the
real `usb-proxy-run` back does nothing, because inittab is still waiting on a
sleep that will not return for 11 days. The appliance then looks broken in a
confusing way: the watch enumerates fine, the log's last lines say "no USB device
attached to proxy yet", and `adb devices` is empty. `ps | grep usb-proxy-run`
does **not** find it either (the process is named `sleep`), so that check gives
false reassurance. Either `kill $(pidof sleep)` after restoring the file, or
write the stub as a short loop (`while true; do sleep 5; done`) so the state
self-heals within seconds. Confirm with `ps w | grep usb-proxy` — the real
`/usr/bin/usb-proxy` process is the thing to look for.

Done that way, DRAM 624 vs 360 gives 0.37 W vs 0.335 W — ~35 mW over a 1.73×
clock change, i.e. **~0.13 mW per MHz**. That is the only trustworthy DRAM number
here; every per-step figure is interpolated from it, and each individual 48 MHz
step (6–10 mW) is genuinely unresolvable on this meter.

**What is left** is the ~0.33 W the board draws on its own (SBC alone, quiescent,
one core at 648 MHz): DRAM refresh, SoC leakage, and buck efficiency at ~65 mA.
`CONFIG_DRAM_CLK` is now 312, taken mainly on stability grounds — §9 already had
it queued as the next margin knob, and at ~20 mW from 480 the power case alone
would not have justified it. Only PLL_CPUX/PLL_DDR/PLL_PERIPH0 are enabled and
every other PLL is off; `BUS_CLK_GATING_REG0` holds only bus-dma, the OTG gadget,
EHCI1 and OHCI1; there is no LED class or thermal zone, and no LEDs are lit at
runtime.

**Dynamic bus clocks (implemented 2026-08-08; power measurement pending).**
Kernel patch `0006` exports the H3 CCU's existing AHB1/APB1 clock IDs and adds a
small board policy driver. `power-tune idle` now changes AHB1/APB1/MBUS to
100/50/150 MHz through the common clock framework; `power-tune active` restores
the exact rates captured at driver probe before CPU1 is brought online. The
interface is:

```sh
cat /sys/devices/platform/usbproxy-bus-clocks/mode
# active ahb1=... apb1=... mbus=...
echo idle > /sys/devices/platform/usbproxy-bus-clocks/mode
```

The targets are deliberately conservative first-step rates, not claimed minima.
AHB2 remains unchanged because it feeds EHCI1/OHCI1 and therefore the proxied
watch; APB2 remains unchanged because it feeds the recovery UART. The driver
validates that all three idle targets are exact CCF-supported rates, verifies
the rates after every transition, and rolls all clocks back if any change
fails. Older/recovery kernels without the sysfs node keep the prior CPU-only
policy. Given the earlier 35 mW result for CPU/core/DE changes, expect a small
single-digit to low-teens mW saving, but treat that only as a test hypothesis:
the wattmeter's ~40 mW drift means it needs the back-to-back method above (or a
higher-resolution meter) before recording a number.

Functional hardware verification on 2026-08-08 passed: the driver probed with
active AHB1/APB1/MBUS rates of 200/100/300 MHz, `power-tune idle` produced the
exact 100/50/150 MHz targets, `power-tune active` restored all three boot rates,
and a second idle transition restored all targets without kernel errors.

**Decode CCU gate bits against the driver, not from memory.** The mmc0 change was
justified by reading `BUS_CLK_GATING_REG0` (0x01c20060) = `0x22800040` and taking
bit 6 for mmc0. Bit 6 is **bus-dma**; mmc0 is bit 8, and bit 8 was already clear,
so that clock was gated the whole time and disabling the node saved nothing —
confirmed on the meter after flashing. The change was kept anyway (an appliance
that never touches the card has no reason to carry the driver, and it does remove
a ~1/s interrupt), but the power rationale was wrong. The Display Engine finding
above *was* checked against `drivers/clk/sunxi-ng/ccu-sun8i-h3.c` and is correct.
Always grep that file:

```sh
grep -n -B1 '0x060, BIT(' drivers/clk/sunxi-ng/ccu-sun8i-h3.c   # and 0x064, 0x068…
```

**With nothing attached, the appliance was busier than when working.** usb-proxy
never starts without a device, so `power-tune boot` left the board wound up at two
cores, and inittab respawned `usb-proxy-run` every 2 s — which forked a shell,
scanned sysfs, and (once it started winding down) forked `power-tune` too. That
measured **2.7 % of a core versus 0.25 % quiescent**, five times busier doing
nothing than the proxy is when actually proxying. `usb-proxy-run` now winds down
in that branch, but only on the transition (guarded on `cpu1/online`), which
brings it to 0.33 %.

**Hazard: do not hammer CPU hotplug.** 50 back-to-back `echo 0/1 >
cpu1/online` cycles hard-reset the board — empty pstore, so a watchdog reset
rather than an oops, almost certainly `stop_machine` starving the 2 s watchdog
feed against its 8 s timeout. 10 cycles spaced 2 s apart were clean. The power
policy cannot reach that rate (transitions are ≥ `power_idle_ms` apart, floored
at 1 s in usb-proxy), but a soak loop written without a delay will reboot the
board.
