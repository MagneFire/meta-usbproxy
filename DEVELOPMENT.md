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
| `--adb_bulk_diag` | opt-in ADB/file-sync bulk-OUT diagnostic logging |
| `--musb_out_read_packets <N>` | bulk-OUT packets per gadget read on musb (default 1; >1 needs the kernel requeue-flush fix) |

The appliance launcher (`usb-proxy-run`) auto-detects the UDC and runs:

```sh
usb-proxy --device "$udc" --driver musb-hdrc \
          --enable_customized_config --auto_remap_endpoints
```

`config.json` (baked into the image) has these keys:

```json
{ "reset_device_before_proxy": false, "bmaxpacketsize0_must_greater_than_64": true,
  "adb_bulk_diag": false, "async_bulk_out_in_flight": 16, "musb_out_read_packets": 8 }
```

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

**Kernel/DT-only update over serial.** Because the runtime rootfs is bundled in
`uImage`, most software changes do not require removing the card. Enter U-Boot,
then YMODEM-write the bundled kernel and DTB directly to the FAT boot partition:

```sh
PI_DEV=/dev/tty.usbserial-XXXX uv run scripts/uboot-console.py --catch
PI_DEV=/dev/tty.usbserial-XXXX uv run scripts/uboot-flash.py \
    --fat uImage /path/to/uImage-initramfs-orange-pi-zero.bin
PI_DEV=/dev/tty.usbserial-XXXX uv run scripts/uboot-flash.py \
    --fat sun8i-h2-plus-orangepi-zero.dtb /path/to/sun8i-h2-plus-orangepi-zero.dtb
PI_DEV=/dev/tty.usbserial-XXXX uv run scripts/uboot-console.py reset
```

The flasher CRC-checks each YMODEM transfer in RAM, writes the named FAT file,
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
logs in as root with the empty password, runs a command, prints the output). It
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
- **`CONFIG_DRAM_CLK=480`** (down from the 624 default). Intermittent boot-time
  kernel oopses with corrupt pointers (e.g. a page-clear faulting at a garbage
  address) are marginal-DRAM / voltage-droop memory corruption, not a software
  bug — likely because the board is powered over the micro-USB host port. 480 MHz
  gives timing margin. Best paired with a solid 5 V supply.

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
evidence, and the diagnostic tooling (`adb_bulk_diag`) that found it.

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
| CPU capped at 816 MHz (1008 OPP deleted) | Removes all rail switching — both remaining OPPs run at a constant 1.1 V. No perf cost: the proxy is USB-RTT-bound and idles at 648 MHz. | kernel patch `0004` |
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

**If corruption recurs at 816 MHz** (check pstore for the signature): the next
knob is DRAM 480→408 in `usbproxy-uboot.cfg` (`CONFIG_DRAM_CLK`); after that,
suspect the 5 V supply path (powered from a host USB port = voltage droop —
use a solid supply).

**Leading theory (2026-08-06, user's, unproven but the best fit): a flaky micro-USB
OTG connector.** Start here before touching another clock. It explains what the
clock changes could not:

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
