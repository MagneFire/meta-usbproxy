# Sustained bulk-OUT limitation on sunxi musb (fastboot boot / large adb push)

Status: **unresolved** as of 2026-07-01. This documents the symptom, everything
the investigation ruled in/out, the current best understanding, and concrete
routes to try next — so a future session can resume without redoing the work.

See also: [`DEVELOPMENT.md`](DEVELOPMENT.md) (build/flash/serial/dev-loop) and the
project memory note `meta-usbproxy-yocto.md`.

---

## 1. Symptom

Through the appliance (usb-proxy on the Orange Pi Zero, sunxi musb gadget), a
**large sustained bulk-OUT transfer stalls and never completes**:

- `fastboot boot <image>` — download stalls (historically ~94% on musb).
- `adb push <largefile>` — stalls early (~10–100 KB), `adb shell` then also hangs
  (ADB multiplexes over the same bulk pipe).

**What works fine:** enumeration, `adb devices`, `fastboot devices/getvar/continue`,
small transfers, the ADB connection handshake. The device also works **directly**
(plugged into the host, no proxy) — see the open control test in §8.

## 2. Reproduction

Topology (the Mac is both the build host and the fastboot/adb host):

```
Mac (adb/fastboot host)
  └─USB─> Orange Pi micro-USB  = musb GADGET (the UDC usb-proxy drives)
          Orange Pi USB-A port = EHCI HOST ──USB──> target device (e.g. watch)
```

usb-proxy forwards Mac→gadget(OUT)→libusb→EHCI→device, and device→EHCI→libusb→
gadget(IN)→Mac. Run `adb`/`fastboot` on the Mac.

Easiest repro (per the device owner): `adb push <bigfile> /data/local/tmp/x`.
Or push a known pattern (`printf`-generated all-`0xAA` file) so content integrity
is trivially checkable.

Watch the proxy log on the appliance: `tail -f /var/volatile/log/usb-proxy.log`
(default prints one line per OUT read/IN write; `-v` adds the `[async]`/`[in]`
traces — see §7).

## 3. What was ruled OUT (with evidence)

The two userspace-fixable classes and the obvious kernel misconfigs are all
eliminated:

| Hypothesis | Test | Result |
|---|---|---|
| usb-proxy OUT pipeline too slow (synchronous per-packet forward) | rewrote OUT path to async libusb (N transfers in flight); on-device A/B sync (depth 0) vs async (depth 8) | **Ruled out** — both stall *identically* |
| musb corrupts OUT content | content-capture: push all-`0xAA`, count non-`0xAA` bytes per OUT read | **Ruled out** — 206/206 512-byte reads pure `0xAA`; only non-`0xAA` were legit ADB `"DATA"` chunk headers |
| musb `rxstate()` spuriously completes reads with 0 bytes | read 6.6.85 `musb_gadget.c` | **Ruled out** — `fifo_count` inits to `packet_sz`; a 0-byte giveback happens *only* on `RXPKTRDY && RXCOUNT==0` = a genuine host ZLP. The 27 observed `read 0` events are real host ZLPs, faithfully delivered |
| RX FIFO double-buffer overrun | read `sunxi.c` FIFO cfg | **Ruled out** — all EPs `MUSB_EP_FIFO_SINGLE(…,512)`, correctly single-buffered |
| usb-proxy forwards host transfer-terminator ZLPs, desyncing the device (re-chunk artifact) | added `drop_zero_len_out` config, on-device A/B | **Did not fix it** — still stalls |

### Key log signatures
- `[in] EP81 receive rv=-7 …` repeating forever = `LIBUSB_ERROR_TIMEOUT`: the
  device **stopped acking** (the terminal state of the stall).
- `read 0 bytes from host` = a genuine host ZLP (RXPKTRDY set, RXCOUNT 0).
- Erratic stall point across runs (23 / 192 / 206 reads) = not deterministic.
- `adb` fails early but `fastboot` reaches ~94%: `adb` checksums **every**
  message (catches loss immediately), `fastboot` verifies only at end-of-download
  (tolerates until the end). Same root cause, different detection point.

## 4. Current best understanding (root cause)

An **erratic packet drop somewhere in the musb path** — either an OUT data packet
or an IN-direction ack (`OKAY`) — causes the ADB flow-control deadlock (host waits
for an ack that never comes; device waits for data it never got). Content-capture
can't see it because a *dropped* packet just means fewer reads, and what is read
is clean.

Why a drop at all, when a correct single-buffered RX FIFO should NAK (lossless)
rather than drop? The prime suspect is the **architecture**, not a one-line bug:

- **sunxi musb is PIO-only** (`sunxi_musb_dma_controller_create()` returns `NULL`;
  the driver forces `VEND0_PIO_MODE`). Every FIFO packet is copied by the CPU.
- **raw-gadget adds a full userspace round-trip per packet**: musb RX IRQ →
  schedule the proxy read thread → `ioctl(EP_READ)` copy → forward via libusb →
  queue the next read. During that window the single 512-byte FIFO is occupied
  and the host is NAKed.
- At sustained High-Speed throughput this latency window is large and frequent.
  Something in that regime drops a packet or trips a protocol timeout, and the
  length-framed ADB/fastboot stream deadlocks.

This matches the project's original suspicion that it is a musb (kernel/HW-level)
limitation, now backed by direct measurement rather than assumption.

## 5. Current code state (committed)

- **usb-proxy `opi` branch:**
  - `e3ed6ac` — async bulk-OUT path, **off by default** (`bulk_out_max_in_flight`
    = 0 = the proven synchronous path). Opt-in via `--bulk_out_in_flight N` or
    config.json `"async_bulk_out_in_flight"`. Kept because it may help controllers
    without the musb RX fragility (dwc2), untested there. Completion callback
    hardened (never calls `libusb_clear_halt` from the event-thread callback).
    `[async]`/`[in]` per-packet traces gated behind `-v`.
  - `drop_zero_len_out` config option (opt-in, default off): skip forwarding host
    bulk-OUT ZLPs. Didn't fix the stall; kept as a documented, controllable knob.
- **Kernel (meta-usbproxy):** patch `0001` (musb RX-requeue) already fixes the
  *low-throughput* requeue-flush drop (cured ADB "offline"); it does **not** cover
  this sustained-throughput case.

## 6. Potential routes to try (roughly by effort / likelihood)

### A. Decisive drop-detection diagnostic *(implemented; run this first)*
Prove **where** the packet is lost before attempting any fix. Enable
`adb_bulk_diag`, push an all-`0xAA` pattern, and watch the stateful diagnostic
that parses ADB WRTE payloads and the file-sync stream on the OUT path. On each
`"DATA"`+`<len>` chunk, it counts the `0xAA` payload bytes that actually arrive.
A `[adbdiag] DATA ... short/non-aa ... remaining=...` line = an **OUT drop** (and
by how much / where); complete DATA chunks followed by a stall = the loss is on
the **IN/ack path**. stdout/stderr are now unbuffered at startup so the log is
live during a stall. Outcome: turns "somewhere" into a specific, targetable
location.

### B. Kernel musb driver work (once the drop is located)
- **RX interrupt / requeue timing.** Study the interaction of the `0001` patch
  (`rxstate()` from `musb_ep_restart`, queue context) with `musb_g_rx()` (IRQ)
  under back-to-back packets. Look for a window where a packet's `RXPKTRDY` is
  serviced/cleared such that the next packet is lost. Instrument `musb_g_rx` /
  `rxstate` with `RXCOUNT`/`RXCSR` traces (ratelimited `pr_*`).
- **Overrun / error flags.** Log `MUSB_RXCSR_P_OVERRUN` / `INCOMPRX` in
  `musb_g_rx` — a bulk overrun there is currently just cleared and ignored
  (`musb_gadget.c` ~L828); if it fires, a packet is being dropped at the HW FIFO.
- **NAK/flush handling.** Verify musb actually NAKs (doesn't ACK-then-drop) when
  the FIFO is occupied and no gadget request is pending.
- Note: **no DMA route** — sunxi musb is PIO-only, so "switch to DMA mode" is not
  available on this SoC.

### C. Reduce/eliminate the raw-gadget userspace round-trip
The per-packet userspace latency is the enabler. Options, increasing effort:
- **In-kernel bridge.** A kernel module that forwards gadget-OUT FIFO data
  straight to the EHCI host (or a functionfs/aio path) without a userspace hop
  per packet. Large effort, but attacks the root cause.
- **raw-gadget batching.** Investigate whether raw-gadget can queue multiple OUT
  reads / larger buffers so musb isn't starved between userspace calls. On musb
  the buffer is clamped to one packet (multi-packet buffers mishandled), which
  limits this.

### D. Different UDC / hardware
- **dwc2/dwc3** (e.g. Raspberry Pi 4). Note earlier fastboot testing saw dwc2
  stall ~96% too, but a **large `adb push` on dwc2 has not been tested** — worth
  doing to confirm whether this is musb-specific or a broader proxy limit. The
  RPi runs the fork's `rpi` branch (dwc2-tuned); `opi` won't enumerate on dwc2.
- A board with a more capable / double-buffered UDC would likely avoid it.

### E. Accept as a documented limitation
The appliance already works for `adb`, `fastboot` control commands, and small
transfers. If large sustained bulk-OUT isn't required, document it as unsupported
on sunxi musb and stop. (This file is that documentation.)

## 7. Diagnostic tooling built during the investigation

- **`-v` traces** (gated behind verbose): `[async] EP01 submitted/complete …`
  (OUT submit/complete + in-flight depth) and `[in] EP81 receive rv=… nbytes=…`
  (device-poll results; `rv=-7` = timeout = device not acking).
- **Content-capture pattern**: push an all-`0xAA` file; a per-read non-`0xAA`
  counter cleanly separates real payload from corruption without offset math.
- **`adb_bulk_diag`** (opt-in): parses ADB WRTE/file-sync DATA chunks across bulk
  OUT reads and logs DATA completions or short/non-`0xAA` shortfalls for the
  all-`0xAA` push diagnostic.
- **`scripts/pi-serial.py`** (uv-run): drive the serial console from the Mac
  (`uv run scripts/pi-serial.py "<cmd>"`); RAM rootfs is writable so config.json
  can be edited on-device and the proxy restarted (`kill -9 $(pidof usb-proxy)`;
  plain SIGTERM hangs the graceful shutdown) — enabling A/B tests **without
  reflashing**. busybox here has **no `base64`**; write files with `printf`.
- **Fast rebuild**: `devtool modify --no-extract usb-proxy <path>` → edit → build
  (see `DEVELOPMENT.md` §5b).

## 8. Open questions / things to verify

- **Control test (recommended):** does a *large* `adb push` complete when the
  device is plugged **directly** into the Mac (no proxy)? Small transfers work
  direct; confirming a *large* one works direct firmly establishes the proxy/musb
  path as uniquely at fault (currently assumed, not verified for large adb push).
- Is there a known sunxi/musb bulk-OUT erratum or Armbian/megous discussion of
  sustained gadget RX loss? (Patch `0002` is from megous — that lineage may have
  relevant notes.)
- Does `adb push` reproduce on dwc2 (RPi4)? (Route D.)
