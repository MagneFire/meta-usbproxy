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

The appliance launcher (`usb-proxy-run`) auto-detects the UDC and runs:

```sh
usb-proxy --device "$udc" --driver musb-hdrc \
          --enable_customized_config --auto_remap_endpoints
```

`config.json` (baked into the image) has these keys:

```json
{ "reset_device_before_proxy": false, "bmaxpacketsize0_must_greater_than_64": true, "adb_bulk_diag": false }
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

### Branches are controller-specific — do not cross them

- `opi` → sunxi **musb** (the Orange Pi appliance). Carries: clamp bulk/interrupt
  OUT reads to one packet on musb, the bulk-IN timeout fix, `_exit` on NO_DEVICE.
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
