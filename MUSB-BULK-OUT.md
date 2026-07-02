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
- **usb-proxy `opi` branch (`ce92f49`, SRCREV-pinned):**
  - `send_data()` bulk-OUT never drops: resends only the unsent tail,
    indefinitely, while the device NAKs (`[outdev]` rate-limited logs);
    `ep_loop_write` logs any fatal send failure instead of swallowing it.
    Correct for any UDC, kept.
  - `adb_bulk_diag` (opt-in, default off): ADB/file-sync stream parser on both
    bulk directions; logs DATA chunk completion/shortfall and ZLP placement.
    This is the tool that found the root cause — keep it.
  - `drop_zero_len_out` and the async bulk-OUT path remain opt-in, off by
    default, unneeded for this bug.

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
  diminishing returns, not pursued.
- Multi-packet reads also passed the historical regression: ADB CNXN
  handshake + enumeration fine (the original failure mode that motivated the
  one-packet clamp).

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
