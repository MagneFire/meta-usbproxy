# Sustained bulk-OUT on sunxi musb (fastboot boot / large adb push) — RESOLVED

Status: **RESOLVED 2026-07-02.** Root cause: `musb_ep_restart()` wrote
`FLUSHFIFO` on every OUT requeue even with `RXPKTRDY` clear; issued while a
packet is in mid-reception this erratically destroys one ACKed packet. Fixed in
kernel patch `0001` (v2): the requeue path now never touches the RX FIFO — it
services an already-pending packet via `rxstate()` and otherwise does nothing,
letting the RX interrupt handle the packet when it completes.

Verified on hardware: 5/5 previously-always-stalling large pushes complete
byte-perfect (4× 10 MB all-`0xAA` + 1× 50 MB random, md5-verified, ~1.2 MB/s),
`adb shell` stays responsive, zero anomalies in any diagnostic layer.

The rest of this file is the investigation record: the symptom, what was ruled
out, the decisive evidence, and the tooling that found it.

---

## 1. Symptom (historical)

Through the appliance (usb-proxy on the Orange Pi Zero, sunxi musb gadget), a
large sustained bulk-OUT transfer stalled and never completed:

- `fastboot boot <image>` — download stalled (historically ~94% on musb).
- `adb push <largefile>` — stalled early (~10–100 KB), `adb shell` then also
  hung (ADB multiplexes over the same bulk pipe).

Enumeration, `adb devices`, `fastboot devices/getvar/continue`, small
transfers, and the ADB handshake all worked. The device also worked directly
(no proxy): the 2026-07-02 control test pushed 10 MB at 9.1 MB/s plugged
straight into the Mac.

## 2. Root cause

**`musb_ep_restart()` flushed the RX FIFO on every OUT requeue.** Stock
mainline code (unchanged in 6.6.85):

```c
} else {
        csr = musb_readw(epio, MUSB_RXCSR);
        csr |= MUSB_RXCSR_FLUSHFIFO | MUSB_RXCSR_P_WZC_BITS;
        musb_writew(epio, MUSB_RXCSR, csr);
        musb_writew(epio, MUSB_RXCSR, csr);
}
```

Two distinct packet-eating modes, both fixed by patch `0001`:

1. **Packet already received (`RXPKTRDY` set):** the flush discards data the
   host had ACKed → host never resends → deadlock. This killed the ADB CNXN
   payload ("device offline") and was fixed first (v1 of `0001`, ex
   `musbfix.ko`): service the packet via `rxstate()` instead.
2. **Packet in mid-reception (`RXPKTRDY` still clear):** per the MUSB
   programming guide `FLUSHFIFO` is *only valid while RXPKTRDY is set*. Written
   while a packet is landing, it either discards the packet outright (it ACKed
   between the RXCSR read and the flush write) or resets the FIFO pointer so
   the packet completes as a **phantom ZLP** — `RXPKTRDY` set, `RXCOUNT == 0`,
   512 bytes gone. This was the sustained-throughput killer.

Why only this appliance: raw-gadget requeues each OUT request from userspace
(outside completion context), so `musb_ep_restart()` — and the flush — ran for
**every single packet**, racing the next back-to-back high-speed packet each
time (µs windows, thousands of packets → erratic hit). Normal gadget drivers
pre-queue several requests, so the path runs only when the queue empties.
dwc2/dwc3 (double-buffered, different driver) never had this code.

## 3. The decisive evidence (2026-07-02 instrumented run)

`adb_bulk_diag` (usb-proxy, both bulk directions) plus kernel `rx-diag` traces
plus a proxy-side never-drop `send_data()` with loud `[outdev]` retry logging —
all in one image, so one run localized the drop:

- Zero `[outdev]` lines → proxy→device (libusb/EHCI) path exonerated.
- Zero kernel OVERRUN/INCOMPRX → no hardware overrun.
- The adbdiag stream accounting for the stalled WRTE #20 (4096-byte payload =
  8×512 + terminator ZLP): the gadget delivered a **0-length read right at
  payload start** (the phantom ZLP — logged mid-payload where a real host ZLP
  is protocol-impossible), then only **7 of 8** data packets, then the genuine
  terminator ZLP, then silence: 512 bytes vanished with a clean read count.
  The phantom sat exactly at the requeue after the short 24-byte WRTE-header
  read — the flush racing the first back-to-back payload packet.

The earlier "read 0 bytes from host" events were previously misread as genuine
host ZLPs; they were a mix of genuine terminator ZLPs (adb sends one after
every 4096-byte WRTE) and these phantoms. `adb` checksums every message so it
stalled early; `fastboot` verifies only at end-of-download so it reached ~94%.

## 4. What was ruled out along the way (with evidence)

| Hypothesis | Test | Result |
|---|---|---|
| usb-proxy OUT pipeline too slow | async libusb OUT path (N in flight), on-device A/B vs sync | Ruled out — identical stalls |
| musb corrupts OUT content | all-`0xAA` push + per-read non-`0xAA` counter | Ruled out — every read byte-clean |
| musb `rxstate()` spurious 0-byte completes | read 6.6.85 source | Partially wrong conclusion: 0-byte giveback *does* require `RXPKTRDY && RXCOUNT==0`, but the flush race manufactures exactly that state |
| RX FIFO double-buffer overrun | read `sunxi.c` FIFO cfg | Ruled out — single-buffered |
| Forwarded host ZLPs desync the device | `drop_zero_len_out` A/B | Ruled out — didn't fix |
| proxy→device drop in `send_data()` (1 s timeout, retry-from-0, drop after 5 attempts) | rewrote to never-drop/resume-tail with loud logging | Real latent bug, fixed in usb-proxy `ce92f49` — but never fired here (0 `[outdev]` lines) |

## 5. Current code state

- **Kernel (meta-usbproxy):** `0001-musb-gadget-service-pending-RX-packet-on-requeue.patch`
  v2 = the complete fix (service pending packet; never flush). The temporary
  `0004` diagnostics patch (OVERRUN/INCOMPRX + phantom-ZLP traces) was removed
  after verification; it lives in git history (`ab34094`..`91ae15a`) if ever
  needed again.
- **usb-proxy `opi` branch (`9518d60`, SRCREV-pinned):**
  - `send_data()` bulk-OUT never drops: resends only the unsent tail,
    indefinitely, while the device NAKs (`[outdev]` rate-limited logs);
    `ep_loop_write` logs any fatal send failure instead of swallowing it.
    Correct for any UDC, kept.
  - `adb_bulk_diag` (opt-in, default off): ADB/file-sync stream parser on both
    bulk directions; logs DATA chunk completion/shortfall and ZLP placement.
    This is the tool that found the root cause — keep it.
  - `drop_zero_len_out` remains opt-in, off by default, unneeded for this bug.
  - `9250e40`: condvar queue wakeups, direct async bulk-OUT submit from the
    read thread, 1 µs timer slack (latency; see throughput round 2 below).
  - `70c9f62`: `adb_ack_accel` — opt-in local WRTE acks for legacy ADB
    (throughput; see round 2 below). The appliance config enables it, plus
    `async_bulk_out_in_flight: 16` and `musb_out_read_packets: 16`.
  - `9518d60`: `adb_ack_accel` pull direction + sized bulk-IN reads
    (see round 3 below).

## 6. Diagnostic tooling built during the investigation

- **`adb_bulk_diag`** (usb-proxy, `--adb_bulk_diag` or config.json): stateful
  ADB transport + file-sync parser across bulk reads, both directions; with an
  all-`0xAA` payload it pinpoints missing bytes to a stream offset. stdout /
  stderr are unbuffered at startup so the log is live during a stall.
- **`-v` traces**: `[async]`/`[in]`/`[outdev]` per-packet paths.
- **all-`0xAA` pattern push**: `python3 -c "open('/tmp/aa.bin','wb').write(b'\xaa'*(10*1024*1024))"`,
  push to a device tmpfs (`mount -t tmpfs -o size=512M tmpfs /mnt/`) if
  storage is tight; md5 both sides.
- **`scripts/pi-serial.py`** (uv-run) for on-appliance log reading and config
  A/B without reflashing (`kill -9 $(pidof usb-proxy)` to restart; busybox has
  no `base64`, write files with `printf`).
- **Fast rebuild**: `devtool modify --no-extract usb-proxy <path>` → edit →
  `bitbake usbproxy-image` (see `DEVELOPMENT.md` §5b).

## 7. Verification record (2026-07-02)

- Control (no proxy, device direct to Mac): 10 MB push OK at 9.1 MB/s.
- Through the appliance, fixed kernel: 4× 10 MB all-`0xAA` + 1× 50 MB random,
  all md5-exact, 1.1–1.4 MB/s, `adb shell` responsive throughout.
- Kernel `rx-diag` during those runs: only genuine host-ZLP lines (one per
  4096-byte WRTE, rate-limited, csr 0003), zero OVERRUN/INCOMPRX, zero
  `[outdev]`, all 640 sync DATA chunks complete.
- `fastboot boot` re-test on the fixed release image: **confirmed working**
  (user, 2026-07-02) — the historical ~94% download stall is gone.

### Throughput tuning (2026-07-02, after the fix)

On-device A/B (10 MB pushes, md5-verified every step):

| Config | Throughput |
|---|---|
| sync, one-packet reads (old default) | 1.0–1.4 MB/s |
| log redirected to /dev/null (spam theory) | no change |
| `async_bulk_out_in_flight: 8` | 2.9 MB/s |
| `async_bulk_out_in_flight: 16` | 3.1 MB/s |
| + `musb_out_read_packets: 8` (4 KB gadget reads) | 3.0 MB/s (50 MB: 2.5 MB/s) |

Findings:
- The old bottleneck was the synchronous per-packet device-side forward
  (~420 µs per 512 B packet). Async in-flight URBs fix that; baked default is
  now 16.
- Multi-packet gadget reads (`musb_out_read_packets: 8`, safe now that the
  requeue-flush is fixed — the old "multi-packet buffers stall" symptom was
  almost certainly that same bug) cut ioctls ~3.3× per WRTE (4096+ZLP+header =
  3 reads instead of 10). Throughput is unchanged because the ceiling moved to
  **ADB's own flow control**: one outstanding 4096-byte WRTE per stream × the
  proxy's ~1.3 ms round trip ≈ 3 MB/s (direct-to-Mac RTT ~0.45 ms ≈ 9 MB/s).
  Kept for the CPU/syscall reduction on the 648 MHz appliance.
- Pushing past ~3 MB/s would need RTT reduction (e.g. the 100 µs sleep-poll
  handoffs between the read/write threads) or ADB burst/delayed-ack mode —
  diminishing returns, not pursued. *(Pursued after all on 2026-07-04 — see
  below.)*
- Multi-packet reads also passed the historical regression: ADB CNXN
  handshake + enumeration fine (the original failure mode that motivated the
  one-packet clamp).

### Throughput tuning, round 2 (2026-07-04): 3 → ~7 MB/s

The device ("Hoki" watch, Android 9 Wear OS) negotiates a **legacy ADB
transport** — empty feature list, 4 KB max payload, one WRTE outstanding per
stream — so throughput is strictly `4096 / RTT`. Delayed-ack is impossible
(adbd far too old; verified `adb features` empty with host adb 37). Two
attacks, both in usb-proxy `opi` (`9250e40`, `70c9f62`):

| Config (10 MB pushes, md5-verified) | Throughput |
|---|---|
| baseline (schedutil) | 2.2–2.9 MB/s |
| performance governor (now in `power-tune`) | 3.2–3.3 MB/s |
| + condvars/fast-path/timerslack (`9250e40`) | 3.7–4.0 MB/s |
| + `adb_ack_accel` (`70c9f62`, ack on payload completion) | 6.6–6.8 MB/s |
| + ack on WRTE *header* (final) | **7.1–7.9 MB/s** (50 MB: 6.9) |

- **RTT trimming (`9250e40`)**: per-endpoint condition variables replace the
  `usleep(100)` queue polls (two of them sat on every WRTE→OKAY cycle); bulk
  OUT submits async directly from the gadget-read thread (one handoff gone);
  `PR_SET_TIMERSLACK` 1 µs. Protocol-agnostic, always on.
- **ADB ACK accelerator (`70c9f62`, opt-in `adb_ack_accel`, baked into the
  appliance config)**: acks host WRTEs locally as soon as the WRTE *header*
  arrives and swallows the device's real OKAYs — the host streams WRTEs
  gaplessly and the remaining ceiling is the datapath itself. Bounded
  spoof-ahead (8), boundary-safe injection, fails open on any framing
  surprise. The file-sync `DONE→OKAY/FAIL` handshake rides in device→host
  WRTEs untouched, so push success/failure is still end-to-end (verified:
  push to a full tmpfs still errors).
- **Hard-won lesson**: old adbd's OKAYs are *not* one-per-WRTE. While its
  transport thread pauses (e.g. forking a shell service), WRTEs pile into the
  stream buffer and are acked with a **single coalesced READY** on drain. A
  FIFO ack-matching first cut deadlocked ~50% of the time under concurrent
  `adb shell` traffic (push stalls, needs kill). Final design is credit-based:
  spoof while per-pair credit (8) lasts, hold the ack at zero (host waits on
  real flow control), and treat any real OKAY as "buffer drained" — swallow,
  refill, release held acks.
- Verification: 8/8 × 50 MB pushes md5-exact **with concurrent shell
  traffic** (6 credit exhaustions, 6 clean recoveries in the log), pull
  md5-exact (device→host direction unaccelerated, ~1.4 MB/s), flag-off run
  behaves like `9250e40` alone (3.9 MB/s), fastboot getvar/reboot through the
  proxy fine, full-tmpfs push still reports the remote error.
- Remaining gap to the 9.1 MB/s direct rate is the proxy datapath itself
  (musb PIO per-packet interrupts); 4-core test showed no gain, 16-packet
  gadget reads ≈ 8-packet. Not worth chasing further.

### Throughput tuning, round 3 (2026-07-04): pull 1.3 → ~4.5 MB/s; fastboot assessed

`adb pull` was still at the unaccelerated rate; the same two costs applied in
mirror (usb-proxy `9518d60`, measured on the Moto 360 "minnow" dev system,
flag-off/on A/B in the same session):

| Config (10 MB pulls, md5-verified) | Throughput |
|---|---|
| `adb_ack_accel: false` (either watch system) | 1.3 MB/s |
| pull-direction accel + sized bulk-IN reads | **4.4–4.7 MB/s** (50 MB: 4.4) |

- **Pull-direction ACK accelerator**: on a device→host WRTE header the proxy
  submits a fabricated OKAY *toward the device* (via the async bulk-OUT path,
  which it therefore requires) and swallows the host's real OKAYs on the OUT
  stream. Same credit model as push (8 credits, hold at zero, any real OKAY
  refills + releases), separate per-direction bookkeeping. Device-bound
  spoofs are only submitted while the host→device parser is at a message
  boundary; `out_feed()` runs before the OUT thread's submit, so the worst
  interleaving is two *complete* messages swapping order (harmless in ADB).
- **Sized bulk-IN reads**: the old IN datapath was one blocking libusb call
  (plus one gadget write) per 512-byte packet — ~9 calls per 4 KB WRTE.
  Mid-payload the accelerator's parser knows the exact remaining byte count,
  so `receive_data()` takes the whole payload in one call. Sized reads can
  time out with partial data (now forwarded, not dropped) and fail open on
  overflow (framing view wrong → back to one-packet reads).
- Verification: 8/8 × 50 MB pulls md5-exact with a concurrent `adb shell`
  loop (140 commands, 0 failures); push regression md5-exact; interleaved
  push/pull fine; interrupted pull recovers; nonexistent-path pull errors
  cleanly; full-tmpfs push still reports the remote ENOSPC end-to-end;
  flag-off run back at 1.3 MB/s baseline; pstore empty throughout.
- **Fastboot: nothing to accelerate.** Its download phase is one command →
  `DATA` response → a continuous raw bulk-OUT stream → one OKAY; there is no
  per-chunk ack ping-pong for an accelerator to hide. Measured through the
  proxy: 8.7 MB in 3.15 s = **2.8 MB/s**, byte-identical at async depth 16
  vs 32, and *slower* than ack-windowed adb push (5.5–7.5 MB/s) through the
  very same bulk-OUT datapath — so the wall is the watch bootloader's own
  USB sink, not the proxy. (A direct-to-Mac `fastboot boot` timing would
  confirm; expected ≈ the same 2.8.) The accelerator correctly fails open on
  fastboot's non-ADB framing (`DISABLED ... spoofed=0` per session) and
  `fastboot getvar`/`boot` work unchanged. Old bootloader quirk: `fastboot
  stage` hangs (unsupported command, absorbed download) — unrelated to the
  proxy.
- Rough remaining pull budget: ~0.85 ms per 4 KB message ≈ device-side
  turnaround + 2 sync libusb calls + gadget writes; a further win would need
  an async bulk-IN pipeline (mirror of `send_data_async`) — diminishing
  returns, not pursued.

## 7b. Fastboot download tail hang (2026-07-16) — RESOLVED

`fastboot boot` of some images hung forever at `downloading '...'` through the
appliance while others worked — image-dependent, 100% reproducible. Root cause
(usb-proxy, not kernel): fastboot's download payload is a raw bulk-OUT stream
with **no terminating ZLP**, so when the image size is a multiple of
wMaxPacketSize the final packets can't complete a multi-packet gadget read via
a short packet. With `musb_out_read_packets: 16` (reads capped at 4096 by
`MAX_TRANSFER_SIZE`), a sub-4096 tail of full 512 B packets left
`usb_raw_ep_read()` blocked forever:

- `twrp-3.1.1-0-minnow.img` (8,026,112 B, mod 4096 = 2048): 1× 17 B
  `download:` cmd + 1959× 4096 B reads, **last 2048 B stuck** → watch never
  OKAYs → host pinned at 'downloading'.
- `zImage-dtb-minnow.fastboot` (8,990,720 B, mod 4096 = **0**): divides evenly
  into 4096 B reads — worked by pure luck, masking the bug for months.

Fix (usb-proxy `opi` `471860a`): the bulk-OUT read loop parses the host's
`download:%08x` command and shrinks subsequent reads to the remaining payload
(rounded up to a whole packet), so the last buffer completes on fill. Logs
`fastboot download of N bytes, sizing tail reads` / `payload complete`.
Verified on hardware: the always-hanging TWRP image now downloads in 2.8 s and
boots to the TWRP UI.

Operational lessons from the debug session:

- **Do not USB-reset a bootloader mid-download.** After an aborted download
  (bootloader still expecting N bytes), every subsequent fastboot command is
  eaten as payload. A `reset_device_before_proxy: true` respawn to un-wedge it
  instead knocked the minnow bootloader off the bus entirely (recovered only
  by replugging the watch cradle).
- **`kill -9` + inittab respawn is invisible to the Mac**: the UDC re-attaches
  within milliseconds, macOS keeps its stale configured device object (same
  ioreg id), never re-enumerates, and the new proxy session waits forever at
  `event: connect` with no bulk threads — looks exactly like "adb broken".
  Neither `soft_connect` toggling nor a 6 s UDC unbind woke the Mac up once
  its port state had wedged (`system_profiler SPUSBDataType` returning empty
  output is the tell); only a physical replug of the Mac↔OPi cable clears it
  (which also power-cycles the OPi — RAM rootfs, swapped binaries are lost).
  **Superseded 2026-07-17:** a wedged port can be cleared *without* replug or
  reboot — `libusb_reset_device` on the stale device (3-line pyusb
  `dev.reset()`) forces macOS to drop the fossil object and re-enumerate.
  Diagnosis of the wedge from userspace: descriptor reads "succeed" (served
  from the macOS cache) while GET_STATUS — never cached — fails with
  `LIBUSB_ERROR_IO` and nothing reaches the proxy log.

## 7c. TWRP adb churn (2026-07-17) — RESOLVED (not the accelerator)

Through the appliance, adb in TWRP 3.1.1 never came up: the watch
connect/disconnected every ~1.3 s on the OPi host port (dmesg device numbers
wrapping past 127) while direct watch↔Mac TWRP adb worked. `adb_ack_accel`
was the initial suspect from an A/B, but a full verbose capture showed **zero
ackaccel activity and zero CNXN in every churn cycle — no ADB traffic ever
flowed**. The A/B had been confounded: the "stable with accel off" soaks ran
against a wedged Mac that never enumerated.

Root cause (usb-proxy, not kernel, not the accel): the proxy forwarded the
host's `SET_CONFIGURATION` to the watch verbatim — but the OPi kernel had
*already* configured the watch at enumeration, and TWRP-era legacy Android
gadgets react to a **duplicate SET_CONFIGURATION** by tearing down and
re-initialising every function. adbd's endpoints die, the gadget
pulse-disconnects (~0.4 s off bus), the proxy exits NO_DEVICE and respawns,
the Mac re-enumerates, sends SET_CONFIGURATION again → loop forever. The
Mac-side `adb devices` stays empty throughout because adb 37.x (libusb
backend) gives up on a device whose serial string read fails mid-churn.
Fastboot and Wear OS adb tolerated the duplicate, which is why only TWRP
broke. Under sustained churn the watch eventually left the bus entirely and
needed a manual reboot.

Fix (usb-proxy `opi` `56aa36a`, pinned by meta-usbproxy `0c8e883`):
`set_configuration()` in device-libusb.cpp queries
`libusb_get_configuration` (answered from sysfs on Linux — no bus traffic)
and skips the request when the device is already in that configuration. This
also leaves both sides' data-toggle state consistent, where forwarding reset
only the device's half. Logs `Device already in configuration N, not
re-sending SET_CONFIGURATION`.

Verified on hardware: TWRP boots via `fastboot boot` through the proxy, the
adb handshake completes, 20 MB `adb push` at 7.1 MB/s and pull at 4.6 MB/s
both md5-exact **with the accelerator active against TWRP's adbd** — the
accel works fine in TWRP and is fully exonerated. Two notes for posterity:
TWRP's CNXN banner is `device::ro.product...`, not `recovery::` (never gate
on banner), and fastboot traffic hitting the accel's ADB parser trips the
intended fail-open (`[ackaccel] DISABLED: unparseable header`) — harmless.

**`adb sideload` in this TWRP is broken device-side (not the proxy).**
Byte-level capture (verbose 2 payload dump, accelerator disabled): the host
opens `sideload-host:<size>:65536`, minadbd OKAYs, and the watch's first and
only sideload message is `WRTE("DONEDONE")` — it never requests block 0
(`"00000000"` appears nowhere). The host adb client correctly interprets
that as "transfer complete" (instant `Total xfer: 0.00x`, exit 0) and TWRP
shows "ADB Sideload Complete failed" on its own screen — its sideload
service gives up internally before requesting any data. Workaround that
works through the proxy: `adb push` the zip (7.1 MB/s) and install it from
the TWRP UI. The wedged-Mac recovery trick is now a script:
`scripts/mac-usb-unwedge.py` (probe mode resets only devices whose
GET_STATUS fails).

## 8. Upstreaming checklist (if submitting patch 0001 to linux-usb)

The fix would benefit any gadget driver that requeues OUT requests outside
completion context (raw-gadget, likely f_fs/aio patterns) on single-buffered
PIO musb hardware. Everything needed to argue the case is in §§2–3 and §7 of
this file; what remains is submission mechanics and one afternoon of
archaeology:

1. **Real Signed-off-by.** The patch carries a placeholder
   (`usb-proxy appliance <usb-proxy@local>`); DCO requires a real name+email.
2. **Rebase onto current mainline and re-test.** Ours is against 6.6.85. The
   region is old and stable so it likely applies unchanged, but verify build
   on `master` and ideally rerun the on-device test with a mainline-ish
   kernel. Target `drivers/usb/musb/musb_gadget.c`, maintainer via
   `get_maintainer.pl` (linux-usb@vger.kernel.org).
3. **Prior-art archaeology (expect this question first):** "the flush has
   been there for ages — what breaks without it?" Run
   `git log -L :musb_ep_restart:drivers/usb/musb/musb_gadget.c` in a full
   clone to find the commit that introduced the flush, and search the
   linux-usb archives (lore.kernel.org) for its rationale. Our answer: with
   RXPKTRDY clear there is nothing to flush; with RXPKTRDY set the packet is
   now *serviced*, which matches what a completion-context requeue does — but
   citing the original intent preempts the thread. This also yields the
   `Fixes:` tag; add `Cc: stable@vger.kernel.org` if backports are wanted.
4. **Exact spec citation.** The commit message paraphrases "FLUSHFIFO is only
   valid while RXPKTRDY is set". Quote the Mentor MUSBMHDRC Programmer's
   Guide RXCSR FlushFIFO bit description verbatim, with register/section
   name, in the commit message.
5. **Self-contained reproducer paragraph** for the commit message /
   cover letter: any Allwinner (sunxi) board in peripheral mode + raw-gadget
   forwarding bulk OUT one packet per request (e.g.
   github.com/MagneFire/usb-proxy, `opi` branch) + a large `adb push` (or any
   sustained bulk-OUT stream) through it; stalls within ~100 KB, reproduces
   100% within a few MB. Direct connection (no proxy) is clean, which
   isolates the gadget path.
6. **Delineate observation vs inference.** Directly observed: a real
   512-byte packet given back as a 0-length request (RXPKTRDY set,
   RXCOUNT==0) immediately after a requeue, mid-payload where a host ZLP is
   protocol-impossible (§3). Inferred from the spec: the mechanism being the
   FIFO pointer reset racing packet reception. Keep that line explicit —
   a reviewer with silicon access may refine the mechanism.
7. **Cosmetics:** `scripts/checkpatch.pl` on the final patch; conventional
   subject (`usb: musb: gadget: don't flush RX FIFO on OUT requeue`); the
   temporary diagnostics patch (`0004`, in git history at
   `ab34094`..`91ae15a`) can be offered in the cover letter as the
   instrumentation used to catch it.
