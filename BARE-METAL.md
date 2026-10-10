# Going bare metal — research notes (2026-10-09)

Question: what would it take to replace Linux + usb-proxy on the appliance
with a single bare-metal firmware for the Orange Pi Zero (H2+), what would
it buy, and is it worth it. Specific to this board and this watch bridge;
nothing here tries to stay generic. Scope decisions taken the same day:
DFU/U-Boot proper are out of scope; the SPL stays as the boot0 for now;
the work is done on a **stock, unrepaired, known-good Orange Pi Zero**,
not the repaired appliance unit — so the UART header is available and the
repaired unit's power-rail history does not apply.

Status: research only, nothing built. Reviewed by Codex (gpt-6.1-sol) the
same day; §9 lists its findings with a verdict on each, and the sections
above it already carry the corrections.

## 0. Short version

- **The boot-time win is small.** The persistent gadget only boots at
  power-on. Today a warm `reboot -f` has the gadget back in 1.13–1.16 s;
  of the SPL's 0.30 s, 0.19 s is the card read of the 3.66 MB uImage,
  which goes away with a ≤100 KB payload. A bare-metal gadget should be
  attached ~0.15–0.25 s after the SPL starts, i.e. ~0.9 s saved per
  power-on — and that is a warm-reboot number; cold power-on → configured
  has never been measured on this rig. Cradle attach → adb improves by
  only ~0.2–0.3 s (the kernel's enumeration overhead and the proxy's
  100 ms poll); the rest of that path is the watch itself and adb's 1 Hz
  rescan on the Mac.
- **The real prize is latency, and it is unproven.** The watch's legacy
  adb keeps one 4 KB WRTE in flight per stream, so a saturated stream is
  bounded by `4096 / (WRTE→OKAY cycle)`. Today that cycle is ~1 ms
  through the appliance with the ack accelerator off (3.7–4.0 MB/s push;
  the 1.3 ms figure in MUSB-BULK-OUT.md predates the condvar/timer-slack
  trims) against ~0.45 ms direct (9.1 MB/s). The accelerator exists only
  to hide that gap. An interrupt-driven bare-metal forwarder with no
  syscalls, no thread handoffs and no libusb *might* approach the direct
  cycle, which would make the accelerator (ACK spoofing; the CNXN/framing
  parser stays) unnecessary and give pull the same rate as push. Nothing
  else in this document justifies the port, so that number is measured
  first — on the current appliance before any code (M-1), then on the
  prototype (M3) — and decides it.
- **Everything the 12 kernel patches fix moves into our own drivers.**
  The specific fixes are a few lines each; the enumeration, disconnect
  and DMA-lifetime handling around them is not. U-Boot proper goes (DFU
  is out of scope); the SPL stays as the boot0 — DRAM init, card read,
  jump — and the firmware is just a different Falcon payload. Recovery
  and the dev loop move to FEL (`sunxi-fel` from the Mac on the OTG port).
- **Stack choice: TinyUSB** (MIT, no RTOS) for the spike, conditionally.
  It has a sunxi musb device driver (`src/portable/sunxi/dcd_sunxi_musb.c`,
  F1C100s, same sunxi register remap as the H3) and an EHCI host driver
  (`src/portable/ehci/ehci.c`) that has run on the H3's EHCI before
  (uli/allwinner-bare-metal) — but the EHCI driver carries NXP-specific
  bits, leaves the interrupt threshold at its 1 ms default and runs one
  transfer per endpoint at a time, so it is an adaptation, not a drop-in.
  CherryUSB has the closer-fitting drivers (explicit `CONFIG_USB_MUSB_SUNXI`
  register map, Allwinner EHCI/OHCI glue, a URB queue) but its host stack
  needs an RTOS thread, semaphore and message queue; it stays the
  alternative if the superloop's completion→rearm latency disappoints.
- **Cost: ~4 weeks for the spike through a first bridge (M-1…M3), 8–12+
  weeks for parity** with what the appliance does today (Codex's estimate
  for parity; I accept it). The stock dev unit has the 3-pin UART header,
  so printf debugging is there from the first instruction — the SPL
  already prints on it — which takes the "nothing visible until musb
  works" risk off the table.
- **Recommendation:** do M-1 now (a day, no code: measure the current
  cycle with `adb_ack_accel: false` and stamp where the ~1 ms goes), then
  M0–M3 as a bounded spike with a hard go/no-go on the measured cycle.
  Do not commit to parity work before the number is in.

## 1. What has to be matched (the appliance today)

Numbers from DEVELOPMENT.md §8–§10 and MUSB-BULK-OUT.md, all measured on
this rig.

| Thing | Today |
|---|---|
| Boot, warm `reboot -f` → gadget back on the Mac | 1.13–1.16 s (SPL 0.30 s, of which card read 0.19 s; zImage inflate; kernel → `/init` 0.216 s; usb-proxy fork → attach) |
| Cradle attach → adb transport (reconnect) | 1.85 s after the kernel's enumeration line, of which 1.0 s is the settle debounce and ~0.9 s adb's own IOKit rescan |
| Bridge throughput (catfish, HS watch) | push 5.3–7.6, pull 6.0–6.3 MB/s with `adb_ack_accel`; without it push 3.7–4.0 MB/s (≈1 ms WRTE→OKAY cycle), pull 1.3 MB/s (pull's sized bulk-IN reads only exist with the accelerator on); 9.1 MB/s direct to the Mac (≈0.45 ms) |
| Image | 3.66 MB uImage (kernel + musl initramfs + static usb-proxy) |
| Kernel patches | 12 (musb RX flush, force-peripheral, DT trims, string-read quirk, bus-clock policy, Falcon DT, SMP without PSCI, arch-timer, EHCI HS retry/reclaim) |
| U-Boot patches | 3 (DRAM pattern size test, `board_usb_init` for DFU, SPL Falcon + watchdog + RTC guards) |
| Resilience | 8 s hardware watchdog from the SPL; panic → reboot; ramoops/pstore post-mortem; RTC GP1 "last Falcon boot died" → U-Boot proper |
| Power | 0.70 W with the watch idle; the whole CPU/clock domain is worth 35 mW |
| Recovery/flashing | U-Boot proper + DFU over the OTG port (`usb-flash-mode`, RTC GP0 `DFU1`); no UART reachable on the appliance unit |
| Operator surface | CDC-ACM root shell on the gadget; `appliance.py swap/boot-ram/flash/check/soak` over it |

What the bridge actually does (bridge.cpp, gadget-fixed.cpp, proxy.cpp): one
fixed gadget `USBPROXY01` (adb iface ff/42/01, fastboot ff/42/03, CDC-ACM
console), never re-enumerated; each slot binds to whatever bulk pair the
watch currently exposes; the idle adb slot NAKs its IN and a sink thread
drains its OUT so the host's CNXN is captured in user space; IN halt pulse
on unbind so the Mac's adb drops its transport; host→device stream kept
framed across binds and the last CNXN replayed to a fresh adbd; fastboot
slot never halted (musb toggle reset); SET_CONFIGURATION not re-sent to an
already configured watch; 1 s settle before a new device is bridged;
queued IN data drained for ~300 ms before a device is dropped (fastboot
sends its OKAY and vanishes); fastboot download tail sizing; the
two-direction ack accelerator. That is the feature list a bare-metal port
must carry, and it is a few thousand lines of logic independent of Linux.

## 2. What bare metal changes, honestly

**Boot.** The SPL stays (it is the only thing that inits DRAM, and the
Falcon path already loads "a Linux legacy image from sector 0x18800 and
jumps to it with the DTB address in r2"). A firmware wrapped with
`mkimage -A arm -O linux -T kernel` is loaded by the unmodified SPL. The
SPL's 0.30 s is ~0.11 s of DRAM init and fixed cost plus the 0.19 s read
of the 3.66 MB uImage; a ≤100 KB payload reads in ~5 ms, and there is no
inflate, no kernel init, no init chain after it. Expect the gadget on the
Mac ~0.15–0.25 s after the SPL starts (PHY reset waits and the Mac's own
enumeration are what remain). Savings ≈ 0.9 s per power-on, and power-on
is the only time the Mac sees this gadget appear. Cold power-on has its
own unmeasured costs (BROM, SD detect, the watch's readiness).

**Latency / throughput (the thesis).** Today one 4 KB WRTE host→watch goes:
musb RX IRQ → raw-gadget completion → usb-proxy read thread wakes →
`bridge_out_feed` → async libusb submit → usbfs ioctl → EHCI qTD → EHCI IRQ
→ usbfs completion → libusb event thread → callback, and the watch's OKAY
comes back through the mirror. Two scheduler handoffs and ~6 syscalls per
direction on a 648 MHz core; ~1 ms per cycle today. Bare metal: musb RX
IRQ → PIO copy of 512 B → write one qTD → EHCI issues it in the async
schedule (several bulk transactions can go in one microframe; NAK
reload and the async-advance doorbell can also add delay); the reverse
is an EHCI IRQ → PIO into the musb TX FIFO. Two things decide whether
that approaches the direct 0.45 ms: (1) **EHCI's Interrupt Threshold
Control defaults to 8 microframes = 1 ms** on the H3 (datasheet p. 493);
Linux programs 1 microframe, TinyUSB leaves the default — the firmware
must set it to 1 while halted or every completion costs up to a
millisecond; (2) the per-packet service cost on the gadget side: at
9 MB/s that is ~17,600 FIFO packet services per second, each an IRQ,
INDEX/CSR accesses on device memory, the copy and the rearm — budget
≤10 µs each or the single core is the bottleneck. The floor underneath
both is the watch's own turnaround, which nobody has measured in
isolation; a programmable bulk responder on the host port (or the watch
with a known echo service) separates bridge latency from watch latency.
If the cycle stays around 0.8–1 ms the port gives nothing here.

**Patches.** All 12 kernel patches become moot, because the driver is ours:

| Kernel patch | Becomes |
|---|---|
| 0001 musb RX FLUSHFIFO on requeue | never flush RX in our driver (the MUSB-BULK-OUT.md lesson, one line) |
| 0002 force peripheral, 0003 DT trim (PHY0 → musb) | PHY0 routing register written once at init (`OTGCTL` bit 0 at `0x01c19420`), ISCR forced ID high / VBUS valid |
| 0004, 0008 CPU cap and 1.3 V rail | no DVFS at all: one core, one clock, rail left where the SPL put it |
| 0005 no mmc0, 0006 bus clocks | nothing to disable; nothing to wind down (35 mW total, not worth code) |
| 0007 skip iConfiguration/iInterface strings | our enumeration never asks for them |
| 0009–0011 Falcon DT, SMP without PSCI, physical arch timer | no DT; cpu1 never started; CNTFRQ set by the SPL patch already, use the physical counter |
| 0012 EHCI HS reset retry + companion reclaim | our port-reset policy: a timed retry state machine, never hand to OHCI (see §5, decision) |

Of the U-Boot patches, 0001 (DRAM size test) and the SPL half of 0003
(Falcon) stay; 0002 (`board_usb_init` for DFU) and U-Boot proper are
dropped. The SPL's GP1 "FLCN" guard keeps working if the firmware clears
GP1 once it is up (power-tune's job today), but its fallback target
changes: instead of loading U-Boot proper the SPL returns to the BROM's
FEL mode, so a firmware that dies *before* clearing GP1 leaves the board
reachable with `sunxi-fel` from the Mac. As today, a wedge *after* the
marker is cleared just gets a watchdog reset and a normal boot; a firmware
that loops through such resets needs its own progress check (§5).

**Robustness.** No scheduler, no page allocator, no `mem_map`, no
`free_initmem`, no maple tree: the decoded corruption oopses were all in
kernel bookkeeping that a bare-metal loop does not have. The appliance
unit's residual fault class (a spilled register coming back wrong; 100/100
clean boots once the rail was pinned at 1.3 V; the AVCC LDO repair on that
PCB) does not apply to the stock dev unit, and a firmware that runs one
core at one clock has no DVFS to provoke it anyway — but keep the SPL's
DRAM size test and 312 MHz, since the SPL misdetection was a stock-driver
flake, not a repair artefact. The watchdog is armed by the SPL and must be
fed from the main loop, and feeding it is not enough: a loop that spins
happily while USB is wedged needs a progress check (packets or SOFs seen
within N seconds while a device is bound) that stops feeding. Lost:
pstore. Replacement: a log ring at a fixed DRAM address (the ramoops
region `0x4fc00000`) with a version/validity header, written through
uncached memory, read by the console `log` command and by a recovery
Linux boot; DRAM retention across a watchdog reset has to be tested, not
assumed. On the dev unit the UART carries the same log live.

**Power.** Nothing to gain; §10 of DEVELOPMENT.md already showed the CPU
domain is 35 mW. A WFI main loop is the natural state anyway.

**Size.** 3.66 MB → tens of KB. Matters only through the card read.

**What is lost that matters.** The Linux USB core's handling of every odd
device behaviour seen so far: the watch's triple enumeration on cradle
attach, the minnow bootloader not answering string reads, TWRP tearing its
gadget down on a duplicate SET_CONFIGURATION, the charger-detection
K-state before a reset, the companion handoff. Each is already documented
with its fix, but each becomes ours to reproduce in enumeration code,
along with the unglamorous parts: address allocation, EP0 sizing, reset
recovery delays, control timeouts, bounds checks on descriptors,
disconnect during any control stage. Also lost: the root shell, `devmem`,
`memtester`. `appliance.py` needs a new transport (a small command set
over the same CDC-ACM interface: `status`, `log`, `fel`, `reboot`,
`speed`). DFU/U-Boot proper are gone: card updates are `dd` with the card
pulled (or, later, an MMC write command in the firmware); iterating is
FEL into RAM.

## 3. The hardware a firmware has to drive

From the 6.6.85 tree in the OrbStack build
(`tmp/work-shared/orange-pi-zero/kernel-source`): the H3 DT
(`sunxi-h3-h5.dtsi`), `ccu-sun8i-h3.c`, `phy-sun4i-usb.c`,
`drivers/usb/musb/sunxi.c`; QEMU's `hw/arm/allwinner-h3.c`; the Allwinner
H3 datasheet v1.2 for the EHCI/PMU bits. Pin these when implementing —
QEMU and other people's ports are references, not silicon validation.

| Block | Base | IRQ (GIC SPI / GIC ID) | Clock gate `CCU 0x060` | Reset `CCU 0x2c0` | PHY |
|---|---|---|---|---|---|
| musb OTG (gadget to the Mac) | `0x01c19000` (regs 0x400) | 71 / 103 | bit 23 (AHB1) | bit 23 | PHY0 |
| USB PHY block | `0x01c19400` (ISCR, PHYCTL at +0x10, OTGCTL at +0x20) | — | PHY clocks `CCU 0x0cc` bits 8–11 | PHY resets `0x0cc` bits 0–3 | — |
| EHCI1 (the watch, USB-A) | `0x01c1b000` | 74 / 106 | bit 25 (AHB2) | bit 25 | PHY1 (PMU regs at `0x01c1b800`) |
| OHCI1 (companion) | `0x01c1b400` | 75 / 107 | bit 29 (AHB2) + 12 MHz `0x0cc` bit 17 | bit 29 | PHY1 |
| EHCI0/OHCI0 (PHY0's host side) | `0x01c1a000` / `+0x400` | 72 / 73 | bits 24 / 28 | bits 24 / 28 | PHY0 — keep off; PHY0 is routed to musb |
| GIC-400 | dist `0x01c81000`, cpu `0x01c82000` | — | — | — | — |
| Arch timer | 24 MHz, CNTFRQ written by the SPL patch; physical timer PPI 30 | — | — | — | — |
| Watchdog | `0x01c20ca0` (the SPL patch writes MODE `0x71` for 8 s; keyed restart via CTRL) | — | — | — | — |
| RTC GP0–GP3 | `0x01f00100`–`0x10c` | — | — | — | — |
| CCU | `0x01c20000` | — | — | — | — |
| UART0 | `0x01c28000`, 16550, 115200 — the 3-pin header on a stock Zero; the SPL already prints there | — | APB2 gate `0x06c` bit 16 | `0x2d8` bit 16 | — |

**SRAM** (QEMU `allwinner-h3.c`, matches the linux-sunxi BROM page): A1
64 KiB at `0x00000000` (the BROM loads at most 32 KiB of SPL into it), C
44 KiB at `0x00010000` (so A1+C is 108 KiB contiguous), A2 32 KiB at
`0x00044000` (U-Boot's PSCI monitor lives there when U-Boot proper runs).
DRAM 256 MiB at `0x40000000` on the appliance unit (512 MiB on a stock
Zero with the larger chip: do not hardcode; the SPL reports it).

**musb, the sunxi flavour.** Mentor MUSBMHDRC with Allwinner's register
remap: FIFOs at `0x00 + 4*ep`; the indexed endpoint window at `0x80`
(TXMAXP/TXCSR/RXMAXP/RXCSR/RXCOUNT, then a 2-byte hole before TXTYPE) —
INDEX writes must not be interrupted by an ISR that also switches
endpoints; POWER `0x40`, DEVCTL `0x41`, INDEX `0x42`, VEND0 `0x43` (write
0 = PIO mode), INTRTX `0x44`, INTRRX `0x46`, INTRTXE `0x48`, INTRRXE `0x4a`,
INTRUSB `0x4c` (must be written back to clear), INTRUSBE `0x50`, FRAME
`0x54`, TX/RXFIFOSZ/ADD `0x90`–`0x96`, FADDR `0x98`, CONFIGDATA `0xc0` but
the H3 has none (`no_configdata`, the driver hardcodes `0xde`), no
TESTMODE, no ULPI. **4 endpoints each direction + ep0, 8 KiB of FIFO RAM**
(`ram_bits = 11`; Linux configures 4×512 single-buffered per direction;
CherryUSB's sunxi table is the same 8192). The fixed gadget needs exactly
4 TX (adb IN, fastboot IN, ACM data IN, ACM notify IN) and 3 RX, so the
hardware fits with one RX spare. PIO only (the sunxi DMA is undocumented
and unused everywhere). FLUSHFIFO on RX is only valid with RXPKTRDY set —
the bug that cost weeks in MUSB-BULK-OUT.md. Linux never sets the
double-packet buffer bit on sunxi and nobody has written down why: ship
single-buffered, try DPB on the adb pair only after the baseline works.

**EHCI1.** A standard EHCI (QEMU emulates it with the generic model) behind
Allwinner's PMU wrapper: HCI_ICR at `+0x800` — write `0x701` = AHB INCR8
(bit 10) + INCR4 (bit 9) + INCRX align (bit 8) + ULPI bypass (bit 0),
what Linux and uli's firmware both write; bit 11 (INCR16) stays clear —
and PHY_CTL at `+0x810` (clear SIDDQ, bit 1 on the H3). The PHY itself
needs the 45 Ω calibration, TX amplitude `0x14` and disconnect threshold
`3` bit-banged through PHYCTL (`sun4i_usb_phy_write`, offsets `0x0c`,
`0x20`, `0x2a`) — copy the Linux sequence. Program USBCMD's Interrupt
Threshold Control to 1 microframe before run (default 8 = 1 ms). Set
CONFIGFLAG so ports route to EHCI. Companion OHCI1 at `+0x400` is a
standard OHCI; only needed if a full-speed device must ever be bridged
(decision in §5), and adding it later is stack-level work (port
ownership, two controllers in one stack), not just a driver.

**Entry state.** The SPL jumps with the MMU and caches off (U-Boot's
`cleanup_before_linux`), in secure SVC, cpu0 only, PLL1 at the SPL's
default (1008 MHz on H3; drop to 816 or 648 — the rail is 1.3 V either
way), DRAM at 312 MHz, the watchdog already running with 8 s, GP1 = `FLCN`,
r2 = the DTB address (ignored). The firmware's first instructions: feed the
watchdog, write GP2 with its own stamp, bring up UART0 and print, install
the vector table and exception-mode stacks with a fault handler that
prints and writes the fault into the log ring and GP3, set up a flat 1:1
page table (16 KiB, TTBCR N=0 — DRAM sits at exactly 1 GiB, so a 4 KiB
N=2 table would push it into TTBR1), D-cache on for code/data, one
uncached (or write-through + explicit maintenance) section for EHCI's
QH/qTD pool and the bulk buffers, GIC with the three IRQs unmasked.

## 4. Software stacks, compared

| | TinyUSB | CherryUSB | U-Boot drivers as a runtime | From scratch | Zephyr |
|---|---|---|---|---|---|
| License | MIT | Apache-2.0 | GPL-2.0+ | — | Apache-2.0 |
| musb device for sunxi | `dcd_sunxi_musb.c` (F1C100s; ~1000 lines; PIO; forced ID/VBUS; HS; F1C100s clock/PHY/IRQ code to replace; assumes 4 KiB FIFO RAM → 8 KiB here) | `usb_dc_musb.c` with `CONFIG_USB_MUSB_SUNXI`: the exact H3 offsets (POWER 0x40 … indexed window 0x80, FIFO cfg 0x90) + `usb_glue_sunxi.c` (4 EP, 8 KiB table) | `musb-new/sunxi.c` + gadget core, proven on this board by the DFU path | the two Linux drivers we already patched are the spec | none |
| EHCI host | `ehci.c`: async + periodic, `hcd_edpt_open/xfer`, has run on H3 EHCI (uli/allwinner-bare-metal, 3 controllers, HID + MSC, IRQ driven). Adaptation needed: NXP-specific port-speed bits and TT register, CONFIGFLAG not set, interrupt threshold left at default, **one transfer per endpoint at a time (one qTD)**, cache hooks to fill in, no barriers | `usb_hc_ehci.c` + OHCI companion option; URB queue per endpoint; Allwinner T113 glue has the same 0x800/0x810/OTGCTL sequence as the H3 | `ehci-sunxi.c` but the API is synchronous (`submit_bulk_msg` blocks up to 5 s): unusable for a two-way proxy without rewriting it | EHCI async list only, ~800 lines — but EP0, enumeration, disconnect recovery and cancellation are the bulk of the work, not the schedule | no EHCI in Zephyr's UHC |
| Raw endpoint host API | `tuh_edpt_open` / `tuh_edpt_xfer` (`CFG_TUH_API_EDPT_XFER`, since 0.14) | URB API, async with callback, zero copy | — | — | — |
| OS requirement | none: `tud_task()` / `tuh_task()` in a superloop, IRQs feed queues; completions are *deferred* to the task, so loop dispatch latency is part of the datapath | **host stack needs an RTOS**: `usbh_hub.c` creates a thread, a semaphore and a message queue unconditionally; osal ports are FreeRTOS/RT-Thread/ThreadX/NuttX/Zephyr/LiteOS-M only | none | none | is the RTOS |
| H3 specifics | nothing (port the F1C100s init) | README claims "AllwinnerTech: all except F1C/F2C (musb + EHCI + OHCI)" but `port/sunxi/` says "get source from official SDK" | board support complete | — | `opi_zero/sun8i_h3` board: UART, GIC, arch timer only; "Not actively maintained"; loaded by U-Boot `go 0x40000000` |

Not candidates: RT-Thread (Allwinner D1/F1C only), NuttX (no H3), xboot (no
H3 USB). uli/allwinner-bare-metal (MIT) is worth mining for its H3 EHCI
bring-up (`usb.c`: gates off → 10 ms → on, "helps with devices not
connecting on boot"), its boot/MMU/GIC/UART code, and as proof that *its*
port of TinyUSB's EHCI driver works on this silicon.

**Why TinyUSB first, and why CherryUSB stays on the table.** CherryUSB's
drivers are the better fit on paper (explicit sunxi register map,
Allwinner EHCI glue, a URB queue that maps 1:1 onto a proxy), but its host
stack is thread-based; the choice would be FreeRTOS on a Cortex-A7/GIC
port or a fake osal, both their own bring-up. TinyUSB runs as a superloop
with IRQ-fed queues. "No scheduler" does not by itself mean lower latency
— TinyUSB defers completion handling to the task, and an RTOS context
switch is tens of microseconds, not a millisecond — so the spike measures
the completion→rearm path on TinyUSB first and switches to CherryUSB +
FreeRTOS if that is where the time goes. From scratch is rejected as the
default: a restricted async EHCI schedule is compact, but EP0,
enumeration, disconnect recovery and gadget state are the larger part and
both stacks have them. Pin the stack commit.

## 5. Design sketch (what "usbproxy-bm" would be)

- **Boot.** Falcon payload at sector 0x18800, same `mkimage` wrapper the
  SPL already accepts. The SPL is the whole bootloader: U-Boot proper, DFU
  and the FAT partition are dropped from the card (the SPL's fallback for
  a bad image or GP1 = "last boot died" becomes a jump to FEL). First
  thing after entry: feed the watchdog, stamp GP2, clear GP1 once the
  gadget is attached (the "boot reached userspace" meaning today).
- **Dev loop and recovery = FEL + UART.** `sunxi-fel spl sunxi-spl.bin`
  (the SPL inits DRAM and returns to FEL — stock U-Boot behaviour),
  `sunxi-fel write 0x40008000 fw.bin` + `exe`: a new firmware runs from
  RAM in seconds with no card writes, over the same OTG port the gadget
  uses. Entry into FEL without a button: card out (the BROM falls through
  to FEL), or the SPL jumping to the BROM FEL entry on an RTC flag (§7.1).
  Card writes only for a release: `dd` with the card pulled. The UART
  header carries the firmware's log and a fault dump throughout.
- **Runtime.** One core, no DVFS, flat MMU map with D-cache, GIC with
  three IRQs (musb ID 103, EHCI1 ID 106, physical timer PPI 30), a 1 ms
  tick for timeouts only — never as the datapath's polling interval — and
  a superloop: `tud_task(); tuh_task(); bridge_poll();` then WFI only when
  nothing is pending (race-free: check with IRQs masked). Watchdog fed
  from the loop *and* gated on a progress check. Log ring in DRAM at the
  old ramoops address, versioned header, uncached writes, mirrored to the
  UART.
- **Gadget.** Same descriptors as today (`USBPROXY01`, same VID/PID/serial,
  adb + fastboot + CDC-ACM) so the Mac's cached device object stays valid.
  Console: not a shell, a command set (`status` = link speed, bound slots,
  uptime, counters; `log`; `fel` = RTC flag + watchdog reset into FEL;
  `reboot`; `speed`), the same command set on the UART. `appliance.py
  check/swap/soak` get a transport for it. Upstream (Mac) and downstream
  (watch) state machines are separate: a Mac reset, SET_CONFIGURATION or
  halt-clear resets local upstream state (toggles, pending transfers) and
  never touches the watch.
- **Host side.** Enumerate whatever appears on EHCI1: GET_DESCRIPTOR
  device/config only (never strings), SET_CONFIGURATION once, find the
  ff/42/01 and ff/42/03 bulk pairs, bind them to the slots. Settle 1 s
  before binding a device that appeared after boot. Port reset policy: a
  timed state machine — chirp, and on "not high speed after reset" retry
  with backoff indefinitely, cancel on disconnect, report the state in
  `status`; **never hand to OHCI** (decision: the only devices this
  appliance bridges are HS; a genuine FS device would then never
  enumerate, which is acceptable and removes the whole 0012 patch class).
- **Datapath.** Gadget-side bulk OUT is read **per packet** (512 B
  transfers), so a fastboot download whose tail is full packets with no
  ZLP can never strand a buffer — the sizing logic is only unnecessary
  under that rule; a 4 KiB gadget read would wait forever on exactly the
  tail MUSB-BULK-OUT.md §7b describes. Each packet is forwarded to the
  watch's bulk OUT as its own transfer. With TinyUSB's one transfer per
  endpoint that means one completion→resubmit per 512 B packet on the
  host side; if M3 shows that gap dominates, the HCD gets a small qTD
  queue (or the firmware batches per adb message using the header length
  it already parses for framing). Per slot one bulk IN transfer pending
  on the watch (4 KiB), completions go to the musb TX FIFO; ZLPs and
  short packets forwarded as-is. On unbind: drain pending IN data to the
  Mac for ~300 ms first (fastboot's OKAY arrives just before the device
  vanishes), then cancel every host transfer, wait for the async-advance
  doorbell, retire the QHs and reject stale completions before the slot
  can bind again — ring and address reuse right after a detach is unsafe.
  Single-buffered musb FIFOs first. The ack accelerator is not ported; if
  M3 shows the cycle did not fall, it can be ported later as it is pure
  adb-stream logic.
- **Bridge semantics ported 1:1 from bridge.cpp:** idle slot NAKs IN and
  drains OUT (CNXN captured, everything else dropped), IN halt pulse on
  adb unbind, fastboot never halted, host→device framing across binds,
  CNXN capture/replay with checksum check, SET_FEATURE halts honoured and
  idempotent.

## 6. Variants considered and parked

- **SRAM-only firmware (no U-Boot at all).** Own eGON boot0, no DRAM
  init, firmware in the 108 KiB of contiguous SRAM. Removes most of the
  SPL's ~0.11 s of retained work and the DRAM flakiness class entirely,
  and the SPL's DRAM code is the only thing the SPL still contributes.
  But the BROM loads at most 32 KiB, so it needs either a ≤32 KiB firmware
  or a second stage with its own MMC read driver; the MMU can stay on
  with a 16 KiB page table in SRAM, but fit, DMA reachability and the
  loader are all unproven. Parked for sequencing only: nothing in M1–M3
  depends on it, so get the cycle number on the SPL route first, then
  decide whether to drop DRAM. **Replacing the SPL later is cheap either
  way**: it is only "DRAM init, read sectors, jump", the payload is a
  plain `mkimage` legacy header a new boot0 can keep loading from sector
  0x18800, and the GP1/RTC-stamp logic is ~20 lines. Own-boot0-with-DRAM
  = U-Boot's `dram_sunxi_dw.c` + H3 tables + our size test (~1200 lines,
  GPL) plus a PIO MMC read; own-boot0-without-DRAM = the SRAM variant.
  The one decision to take now so that the SRAM variant stays open: keep
  the firmware's RAM footprint under ~70 KiB (EHCI pool, rings, no large
  log ring) and put DRAM addresses only in the linker script.
- **U-Boot proper as the runtime.** Its musb gadget is proven here, but its
  host stack is synchronous; writing an async EHCI path inside U-Boot is
  the same work as the bare-metal one with a worse build system.
- **Zephyr.** Gives threads, GIC, timer and a UART driver on this exact
  board, but every USB driver would still be ours and the board port is
  unmaintained. Only worth it if the superloop turns out not to be enough
  — and then CherryUSB + FreeRTOS is the shorter route.

## 7. Risks and unknowns

1. **Debug access.** The stock dev unit has the UART header, so the
   firmware prints from its first instruction and the SPL's own banner
   proves the cable before any firmware exists. FEL is still the dev
   loop: the SPL can jump to the BROM's FEL entry on an RTC flag (the
   same trick as `DFU1`; a SUSE U-Boot patch does exactly this for the H3
   with entry `0xffff0020` — verify against the BROM before relying on
   it), after which `sunxi-fel write/exe/read` from the Mac over the OTG
   port loads code and dumps memory with no card writes. When the
   firmware later moves to the appliance unit (no UART), the CDC console
   and the log ring are all there is — keep both first-class from M1.
2. **The thesis may be wrong.** The direct 0.45 ms is a baseline, not the
   bridge's floor: a second bus, serialization, two independently phased
   host schedules, EHCI's interrupt threshold, TinyUSB's deferred
   completions and the per-packet host resubmit all sit between. If the
   cycle stays ~0.8–1 ms, throughput stays ~4 MB/s without the
   accelerator and the port bought boot time and a small image. M-1 and
   M3 are the go/no-go.
3. **musb PIO service cost on a Cortex-A7.** ~17,600 packet services/s at
   9 MB/s, each an IRQ plus device-memory register traffic. Measure the
   whole service (IRQ → copy → rearm) and IRQ occupancy at M1 with a
   loopback, not just a 512 B copy.
4. **Enumeration corner cases are ours now**: triple enumeration on cradle
   attach, bootloader string timeouts, the cold-boot chirp miss, the
   charger K-state, macOS's stale device object (keep ≥1 s D+ low between
   attaches), disconnect in any control stage. Each is documented in
   DEVELOPMENT.md with its rule.
5. **Detach is a DMA-lifetime problem** (§5): stale EHCI completions,
   half-retired QHs and old-device data leaking into a new binding are the
   bugs a superloop makes easy to write.
6. **Recovery cannot depend on CDC working**: a wedged USB stack with a
   fed watchdog is a brick until power-cycle. The progress check (§5) and
   the FEL path are the mitigation; both need a deliberate test.
7. **Two boards, two behaviours.** The dev unit is stock; the appliance
   unit is repaired (AVCC via a bodged LDO, header-powered) and 256 MiB.
   Whatever passes on the dev unit has to be soaked on the appliance unit
   before it is trusted there, and the DRAM size must come from the SPL,
   not a constant.
8. **Double buffering on sunxi musb is untested anywhere we know of.**
   Single-buffered first.
9. **Operator tooling** (`appliance.py`, the appliance skill, the soak
   harness) needs a new transport; budget a week.

## 8. Milestones (each ends with a number)

| | Scope | Proves | Time |
|---|---|---|---|
| M-1 | No code. On the current appliance with `adb_ack_accel: false`: paired direct / appliance runs of uncompressed random push and pull to tmpfs, md5 both sides, with and without concurrent `adb shell` traffic; `[uptime]`-stamp the proxy's receive→submit and completion→write gaps so the ~1 ms cycle is split into kernel, proxy and watch shares | where the time goes today; whether a cheaper Linux-side change recovers most of it; the baseline M3 is compared against | 1 day |
| M0 | On the stock dev unit with the UART attached: FEL recovery proven first (known-good Linux card image kept; SPL fallback retargeted from U-Boot proper to FEL; `sunxi-fel` round trip from the Mac). Then a Falcon payload with UART printf, vector table, exception stacks and fault capture into GP3, MMU + caches, GIC + timer, LED blink, GP2 stamp, GP1 clear, watchdog feed — and deliberate failure tests (hang before the GP1 clear → FEL; hang after → watchdog reboot) | the SPL → firmware path, the FEL dev loop, the GP1 fallback and the runtime skeleton | 2–4 days |
| M1 | musb device via TinyUSB `dcd_sunxi_musb` ported to the H3 (clocks/PHY/IRQ, 8 KiB FIFO, 4 EP): the fixed gadget enumerates on the Mac with the CDC-ACM console echoing, **plus** a bulk loopback endpoint pair driven from the Mac with pyusb: full packets, short packets, ZLPs, sustained duplex; IRQ occupancy and per-packet service time from the physical counter | the gadget and the PIO budget (§7.3) | 1 week |
| M2 | EHCI1 host via TinyUSB `ehci.c` adapted to the H3 (ICR/PHY, ITC = 1, CONFIGFLAG, cache hooks): enumerate the watch on a cold boot with it cradled, HS reset-retry state machine, link speed in `status`; bulk transfers to/from the watch's adb endpoints, cancellation and clean disconnect mid-transfer | host side works and can be torn down | 1–1.5 weeks |
| M3 | Clean-session bridge on the adb slot only (no replay, no mode switches); the M-1 experiment repeated on the prototype, plus physical-counter stamps at musb RX, EHCI submit, EHCI completion, musb TX and loop dispatch, recorded as distributions | **go/no-go**: mean 4096-byte WRTE→OKAY cycle approaching 0.5 ms (≥8 MB/s with no accelerator) → continue; ~0.8–1 ms → stop | 1–2 weeks |
| M4a | Parity: fastboot slot, bind/unbind with drain and cancellation, idle sink, CNXN replay/framing, halt pulse, settle, log ring, console commands, `appliance.py` transport | feature parity | 3–4 weeks |
| M4b | Fault injection and soak: unplug mid-header/mid-payload, adb↔fastboot↔TWRP cycles, fastboot OKAY→disconnect, Mac sleep/resume and reset, long idle → rebind, simultaneous push/pull, 100 warm + 20 cold boots — on the dev unit, then repeated on the appliance unit | ships | 2–3 weeks |

Total: ~4 weeks to the go/no-go, 8–12+ weeks to parity.

## 8a. M-1 results (2026-10-09, appliance unit, Hoki watch at 480 Mb/s)

`usb-proxy --cycle_trace` (trace.cpp, opt-in, 50 µs bins, paired per stream
pair) swapped into the running appliance, `adb_ack_accel: false`, 20 MB
random pushes/pulls to a tmpfs on the watch via `scripts/cycle-bench.py`,
md5-exact every run. Logs: `~/.cache/appliance/m-1-*.log`, rows in
`~/.cache/appliance/cycle-bench.tsv`. The watch's idle load average is ~2.3
(its normal state, not "busy").

| Run | push MB/s | pull MB/s | push cycle | pull cycle |
|---|---|---|---|---|
| accel on (reference, loop device for /tmp) | 7.4 (5.8/8.3/8.1) | 6.2 | ~0.55 ms | ~0.66 ms |
| accel off, 16-packet gadget reads | 3.60 | 1.47 | 0.94–0.98 ms | 2.37–2.45 ms |
| accel off, 1-packet gadget reads | 3.57 | 1.50 | 0.99–1.07 ms | 2.15–2.38 ms |
| accel off, 16-packet, concurrent `adb shell` loop | 3.80 | 1.50 | 0.92–0.94 ms | 2.1–2.4 ms |
| direct (watch on the Mac, tmpfs) | 7.77 (7.1/8.0/8.2) | 7.20 | 0.50–0.58 ms | 0.56–0.58 ms |
| direct, concurrent `adb shell` loop | 7.97 | 7.17 | ~0.51 ms | ~0.57 ms |

**What M-1 says (2026-10-09).** The direct cycle on this Mac/watch pair is
~0.52 ms push / 0.57 ms pull (the 0.45 ms in older notes was a different
session), i.e. the watch itself tops out near 8 MB/s. The appliance with the
accelerator on already sits within ~10 % of that (7.4 / 6.2 MB/s). With the
accelerator off the proxy+kernel path adds **~0.43 ms per push cycle** (0.95
vs 0.52) and **~1.8 ms per pull cycle** (2.4 vs 0.57, the sync one-packet IN
reads). A bare-metal datapath therefore has ~0.4 ms per cycle to remove on
push and would have to get its own cost under ~0.05–0.1 ms to reach parity
with direct; the split below says where the 0.4 ms sits (two user-space hops
~110 µs, usbfs/EHCI/libusb ~150 µs, the single-buffered OUT re-arm and PIO
stalls ~150–200 µs). Honest framing for the M3 gate: the *throughput* gain
over today's shipping appliance (accelerator on) is at most ~10–15 %; what
the port buys is reaching that number without the ACK-spoofing accelerator
and the same rate on pull as on push. The gate stays as written (mean
4096-byte cycle approaching 0.5 ms with no accelerator), now anchored to the
measured 0.52 ms direct.

**Push cycle split (accel off, 16-packet reads, p50 of 1024 cycles):**

| Gap | µs | What it is |
|---|---|---|
| WRTE header read → payload read done | 325 | the Mac's header→payload URB gap **plus** the gadget side: single-buffered RX FIFO, the proxy re-arming the OUT read, musb PIO per packet; bus time for 4 KB at HS is ~70 µs |
| payload done → libusb submit | 25–40 | proxy user space |
| submit → completion callback | 225 | usbfs + EHCI schedule + IRQ + libusb event thread; ~70 µs of it is bus |
| completion → OKAY read from the watch | 275–325 | the watch's turnaround + its 24-byte IN + a blocking libusb read returning |
| OKAY read → OKAY written to the gadget | 75–125 | proxy handoff to the write thread + raw-gadget write |
| OKAY out → next WRTE header | 125 | the Mac |

Reading: of ~950 µs, the Mac's own gaps are ≥ ~250 µs (125 after the OKAY
plus an unknown part of the 325), the watch ~250–300 µs, and the
proxy+kernel path ~400–500 µs (the re-arm/PIO part of the first gap, the
~150 µs of non-bus time in submit→complete, and the two user-space hops).
That is the share a bare-metal datapath can attack; the direct run bounds
the Mac+watch share from the other side.

**Pull cycle split:** 2.0 ms of the 2.4 ms is "WRTE header → payload read
done": with the accelerator off the IN path is one blocking libusb read per
512-byte packet (~250 µs each × 8). The accelerator's sized reads are what
make pull usable today; a bare-metal IN path (one 4 KB transfer pending)
removes this entirely, so pull should match push there. The remaining pull
gaps: payload → gadget write 75–125 µs, host OKAY read 175 µs after it,
OKAY submit+complete to the watch 25–75 µs, watch's next header 275 µs.

**1-packet reads** (the shape closest to the bare-metal per-packet OUT
path) cost ~100 µs more on push (header→payload 475–550 µs): eight
raw-gadget reads and eight async submits instead of one each. In the
firmware the per-packet work is an ISR copy and a qTD, not a syscall pair,
so this is not predictive, but it says per-packet handling must be cheap.

**Trace caveat:** the `submit→complete` mean occasionally shows a huge
outlier (a stale stamp when a WRTE's payload-done event was not paired);
use p50/p90. Fix pending in trace.cpp.

## 8b. M0/M1 bring-up log (2026-10-09/10, stock Zero, 256 MiB)

- **Dev loop works as planned**: card = appliance wic with sector 0x18800
  zeroed (done from U-Boot: `mw.b` + `mmc write`), SPL → "Expected image is
  not found" → U-Boot proper, `appliance.py bm run fw.bin` = key-spam catch
  + `loady 0x40008000 750000` (24 KB in ~1 s) + `go`. The appliance kernel
  has no UART driver, so `ub_catch_prompt` now reboots it over the USB
  console when the dongle is silent.
- **Set/way clean does not land the image in DRAM on this A7.** After
  `loady` the image sits dirty in U-Boot's D-cache; a textbook DCCISW
  set/way pass over CLIDR LoC (both levels) followed by caches-off left DRAM
  holding the *previous* build beyond the first few hundred bytes (read back
  with the caches off: `0x447e684c` where `e59f31b8` belonged), so `bl main`
  jumped into stale code. A DCCIMVAC clean-by-address over the image range
  before the caches go off fixes it; both are kept in `start.S`. The
  invalidate-only set/way pass with the caches already off hung the core
  and was dropped (one clean+invalidate pass is enough).
- **DesignWare UART LCR**: a write while USR.busy is ignored and can leave
  DLAB set (output died after `uart_init`); `uart_init` now drains and
  retries LCR until it reads back (Linux `dw8250_check_lcr`).
- **Secure-state timer**: in secure PL1 the CNTP_* registers drive the
  secure physical timer, PPI 29 (not 30). Both enabled.
- **M1 on the first run**: TinyUSB `dcd_sunxi_musb` forked for the H3 (base
  0x01c19000, CCU bits 23/8, 8 KiB FIFO, no SRAMC, PHY0 route via OTGCTL,
  SIDDQ clear in PMU0) enumerated on the Mac at high speed straight away:
  `/dev/cu.usbmodemUSBPROXYBM1`, `adb devices` → `USBPROXYBM offline`, the
  idle adb slot received adb's 310-byte CNXN, the CDC console answers
  `status`. Firmware 24 KB.
- **M1 exit (tools/loopback.py, adb stopped, pyusb on the adb slot):** full
  512-byte packets, 1/7/64/511-byte short packets and a zero-length packet
  all echo exactly; 5000 × 512 B ping-pong, 0 mismatches, md5-exact,
  **181–189 µs per host→firmware→host round trip** (2.7–2.8 MB/s each way in
  strict ping-pong, i.e. with the Mac's synchronous libusb turnaround in
  every cycle). OUT flood with the loopback off: **17.6 MB/s, 27.7 µs per
  packet at the gadget, 16384/16384 packets counted** — the ISR → `tud_task`
  → re-arm handoff on a single-buffered FIFO; twice the 8 MB/s the watch
  path needs, so good enough for M3, with re-arming from the ISR and the
  double-packet buffer as the known next steps if the M3 split says so.
  Found en route: upstream `dcd_sunxi_musb.c` never sends a zero-length IN
  transfer (returns "complete" without TXPKTRDY); fixed in the fork.

- **M2 (2026-10-10, 00:00–00:50): the watch over EHCI1.** TinyUSB's
  `ehci.c` forked (`src/usb/ehci_h3.c`): port speed from PORTSC, no NXP TT
  register, busy-endpoint guard, companion-handover flag — and two upstream
  bugs in its standard-EHCI branch, which NXP's ChipIdea targets never
  compile: a frame-list macro that does not exist, and the completion ISR
  walking periodic intervals up to `FRAMELIST_SIZE` (256) when only four
  period heads exist (1/2/4/8 ms) — it ran off `period_head_arr[]` on the
  first completion and hung the core with IRQs masked (found with RTC GP0
  breadcrumbs across the watchdog reset: last word written = ISR #2
  entry). Glue (`hcd_h3_ehci.c`): PHY1 (PMU1 ICR 0x701, SIDDQ clear,
  tuning), HCRESET, ITC = 1 µframe, CONFIGFLAG, port power, GIC 106.
  Result: the watch (18d1:d001) enumerates at **high speed in 35 ms / 8
  interrupts**, interface 0 (ff/42/01) binds to host slot 0 (0x81/0x01),
  mounted ~270 ms after firmware entry. adb CNXN exchange from the console:
  header 24 B in 54 µs, payload 7 B in 91 µs (adbd drops a combined
  31-byte packet — header and payload must be separate transfers, as adb
  and the appliance send them), the watch's CNXN reply (legacy transport,
  maxdata 4096) 4.7 ms later, its 63-byte banner read in 61 µs. Idle: ~4
  IRQ/s (256 ms frame-list rollover). Firmware 39.8 KB. Still owed for the
  M2 exit: a cold cradled boot from the card (Falcon), attach/detach cycles,
  abort-on-unplug.

- **M3 first cut (2026-10-10, 01:00–01:30): adb through the bare-metal
  bridge.** `src/bridge/bridge.c`: two 4 KiB buffers per direction per
  slot, messages held until a watch binds. First run: `adb devices` showed
  the watch as `device` through the firmware 2 s after boot; a 20 MB push
  went through md5-exact at 2.6 MB/s, but every pull died with adb's
  `connection terminated: read failed` — the watch's adbd sends no ZLPs,
  so a payload that is a multiple of 512 B and < 4 KiB merged with the next
  header in a whole-buffer read and the Mac's 24-byte header read overran.
  Fixed with adb-aware sized IN reads (header, then `data_length`), plus
  dropping the Mac's terminator ZLPs toward the watch. Result, twice,
  md5-exact both ways: **push 4.7 MB/s (≈0.87 ms/cycle), pull 3.5 MB/s
  (≈1.17 ms/cycle)**. For reference: Linux appliance accelerator off 3.6 /
  1.5, accelerator on 7.4 / 6.2, direct 7.8 / 7.2. Per-4 KiB costs now:
  hand-off 1 µs; 138 µs to drain into the watch (the watch's own OUT
  acceptance, same as Linux saw); 172 µs to drain into the Mac = musb PIO
  at ~21 µs per packet, now the dominant firmware-side cost. Not at the
  0.5 ms gate yet, but above Linux-without-accelerator with zero
  optimisation. Known next steps, in order of expected payoff: cached
  (instead of uncached) buffers for the musb PIO side with explicit cache
  maintenance for EHCI; musb double-packet buffering on the adb pair;
  queueing the payload transfer behind the header on the watch OUT (a
  small qTD queue in the EHCI fork) instead of a task hop between them; a
  proper stamp histogram (stamps.c) to split the remaining ~0.4 ms. M3
  gate measurement (paired runs, shell loop, distributions) still to do.

- **M3 datapath optimisation (2026-10-10, 06:50–08:00, usbproxy-bm
  3ac2b4a → 249f33f).** Same rig, 10 MB ×2 per point, md5-exact throughout.
  Result: **push 5.35 MB/s (0.77 ms/cycle), pull 6.05 MB/s (0.68 ms)** from
  4.7/3.5; direct is 7.8/7.2 (0.52/0.57 ms), so the firmware now adds
  ≈0.25 ms on push and ≈0.1 ms on pull. What moved it, in order found:
  1. *Cached bridge buffers* (pull 3.5 → 4.5). The musb PIO loop read the
     uncached `.dma` section one 32-bit word at a time, a DRAM round trip
     per word. Buffers are now cached, whole cache lines, with DCCMVAC /
     DCIMVAC by address around the EHCI DMA (`hcd_dcache_*` strong
     definitions; no-ops inside `.dma`). Measured PIO cost after: 10.0 µs
     per 512 B FIFO read, 2.8 µs per write (FIFO register reads are the
     slow side).
  2. *musb double packet buffering on the adb pair* — no measurable gain
     by itself, three bugs to get it working: the RX service loop must stop
     when no transfer is armed (RXPKTRDY is deliberately left set as
     back-pressure; the first cut spun in the ISR until the watchdog); the
     second buffer's RXPKTRDY re-asserts late and its interrupt can be
     folded into the first's, so FIFOFULL is sampled before the release;
     and the TX path must never load the FIFO while TXPKTRDY is set (Linux's
     `txstate()` guard) — once per 4 KiB transfer the IRQ for packet k−1
     arrived with both buffers full and the write corrupted the stream (Mac
     `usb_read` failed with kIOReturnOverrun on every pull). Also replaced
     upstream's best-fit FIFO allocator with a static size-aligned map: an
     unaligned 1 KiB double buffer, and a layout with FIFOs at ≥5120,
     both produced garbage on the CDC console endpoints; offsets up to 4608
     verified. The H3 FIFO RAM is not simply 8 KiB flat.
  3. *Conditional `wfi`* (OKAY-to-Mac leg 99 → 12 µs). The main loop slept
     unconditionally after each pass, so any completion raised during the
     task pass waited for the next interrupt — up to one SOF period,
     125 µs. Now: IRQs masked, sleep only if both TinyUSB queues are empty
     and no EHCI transfer is in flight.
  4. *EHCI completion polling* (pull 4.65 → 5.75, OKAY-to-watch leg 125 →
     21 µs). Interrupt Threshold Control cannot go below one microframe, so
     every EHCI completion IRQ was quantised to a 125 µs boundary — a 24-byte
     OUT to the watch cost ~120 µs, and splitting payloads into smaller
     transfers made things *worse* (each transfer paid it). The main loop
     now walks the async QHs for retired qTDs (EHCI IRQ masked). This
     exposed a latent bug in TinyUSB's `qhd_xfer_complete_isr`: it checks
     only the QH overlay's Active bit, which still shows the previous
     transfer's 0 between `qhd_attach_qtd()` and the HC's advance-queue;
     the poll completed transfers that never ran (enumeration hung on the
     status stage). The check now also requires the qTD itself retired.
  5. *2 KiB chunks* (push 5.0 → 5.35, pull 5.75 → 6.05). With completions
     cheap, forwarding each 4 KiB payload as two transfers overlaps the
     second half's arrival with the first half's delivery. 1024/512 are
     worse again: every transfer boundary costs a NAK round on the musb
     side (RXPKTRDY back-pressure until the next read is armed) and a qTD
     on the EHCI side. Runtime knob `chunk N` on the console.
  Cycle stamps (`status` → `cycle_push` / `cycle_pull`, means over all
  cycles, µs, chunk 2048): push header→payload in 212, →delivered to watch
  109, →OKAY from watch 378, →OKAY delivered to Mac 16, Mac turnaround ≈65;
  pull header→payload in 201 (the watch's own sending pace), →delivered to
  Mac 52, →OKAY from Mac 119, →OKAY delivered 21, watch turnaround ≈285.
  adb keeps exactly one WRTE outstanding (in-flight depth 0 at every
  header). The watch's own turnaround (≈380 µs on push, ≈285 on pull) is
  the largest single term and is in the direct numbers too.
  Where the remaining ≈0.25/0.1 ms is and what would take it: (a) the Mac's
  4 KiB arrives into musb at ~25 µs/packet (200 µs) versus ~100 µs into the
  watch's UDC directly — musb DMA mode 1 (one interrupt per transfer, wire
  speed, no PIO) is the fix, Allwinner's USB DMA block, not yet written;
  (b) true cut-through — forward packets as they land inside one large read
  instead of chunk boundaries: a qTD queue on the EHCI OUT side, overlay
  progress on the EHCI IN side — would take the delivery legs (109/52) to
  ~20. Together those put push around 0.58 ms and pull around 0.6 ms, i.e.
  within ~10% of direct. **Gate reading:** the plan's 0.5 ms / ≥8 MB/s
  target was written against the 0.45 ms direct estimate; the measured
  direct is 0.52/0.57 ms (7.8/7.2 MB/s), so ≥8 MB/s is not reachable by
  any proxy. Against the real ceiling the firmware is at 69% (push) / 84%
  (pull) of direct with two identified, mechanical steps left.
  Tooling lessons: the build id is a generated `build/gitrev.h` (a `-D`
  left stale strings in a newer binary; a phony-driven header rule never
  retriggered under Apple's make 3.81); the loopback flood stage is opt-in
  (with the bridge present it fills the 4 buffers and stalls); the watch
  physically dropped off the cradle twice this session — Linux on the card
  sees only root hubs in that state, which is how to tell it from firmware.

- **M3, the two follow-ups (2026-10-10, 08:00–09:00, usbproxy-bm → 'M3:
  watch->Mac cut-through, musb inner DMA').** Result: **pull 6.2–6.3 MB/s
  (0.65 ms) steady; push 5.8–7.0 MB/s warm (0.59–0.71 ms)**, 4.3 on the first
  run after the watch has idled. Direct 7.8/7.2. Both deliverables built;
  one of them turned out not to be the lever:
  - *Cut-through, watch→Mac* (pull 5.75 → 6.2): packets of an in-flight
    EHCI IN read are forwarded to the Mac as they land, using the QH
    overlay's remaining-byte count, held one packet behind. Two traps: the
    qTD slot is reused, so a matching current-qTD pointer still describes
    the *previous* transfer until the HC fetches the new one (4 KiB of stale
    data went out); and the overlay count runs ahead of the data in DRAM —
    64–128 stale bytes at packet starts, tapering with offset, md5 mismatch —
    so the newest packet is never touched. Delivery leg 97 → 25 µs.
  - *Cut-through, Mac→watch*: built, measured, **off**. Forwarding each
    512 B packet as its own EHCI OUT made the watch's OKAY arrive ~200 µs
    later (sent→OKAY 378 → 579 µs); the watch's UDC likes bursts. Store-and-
    forward of whole 4 KiB messages is faster for push.
  - *musb inner DMA* (Allwinner USB DMA, 8 channels at +0x500/+0x540; CSR
    sequences from the vendor `usbc_dev.c`): works (30 k transfers,
    md5-exact, short-packet abort via the residual count) but **moves no
    latency**: the Mac's 4 KiB still arrives in ~215 µs (≈100 µs is the
    Mac's own gap between its header and payload writes, the rest ~12–15 µs
    per packet of host pacing — the inter-packet histogram shows most gaps
    under 20 µs with PIO too). RX DMA stays on for the CPU it frees; TX DMA
    was slower than PIO (137 vs 99 µs per 4 KiB) and is off.
  - EHCI NAK reload 0 vs 4 (live knob): no difference.
  Where the cycle is now (warm watch, µs, chunk 4096): push 215 arrival +
  122 delivery + 243 watch + 16 → 585–705 depending on the watch's mood;
  pull 182 arrival (watch pace) + 26 delivery + 140 Mac + 22 → 650, with
  the watch's own send turnaround ≈290. **The watch's state is now the
  largest variable**: the first run after idle sees its USB acceptance and
  turnaround slow 2–5× (one run: 2.2/2.5 MB/s), then it settles. Direct
  would see the same, and the M-1 direct numbers were warm.
  What is left to close, honestly: the proxy's irreducible cost is one
  packet time plus a hop per direction (~20–30 µs per leg), which is where
  the pull side now is. Push still carries the whole-message store-and-
  forward (~100 µs of arrival the watch would otherwise overlap) because
  the watch punishes per-packet delivery; the middle ground — forward in
  2 KiB halves once 2 KiB have landed (cut-through with a 2 KiB floor rather
  than per packet) — is untested and is the last mechanical step. Beyond
  that only the accelerator (local OKAY) beats direct, which is exactly what
  the bare-metal spike set out not to need.

- **The 2 KiB-floor experiment (2026-10-10, 09:10).** Mac→watch cut-through
  forwarding in 2 KiB halves (progress from the PIO count or the RX DMA
  residual, one packet back) cut the delivery leg from 120 to 40 µs — and the
  watch's OKAY arrived at exactly the same absolute time (header→OKAY
  583–601 vs 588–609 µs; push 6.7–7.0 MB/s either way, A/B twice on one
  boot). The watch's turnaround is not bounded by when its last byte lands.
  Off by default. **M3 closes here**: warm watch, push 0.585–0.61 ms
  (6.7–7.0 MB/s, 86–90% of direct), pull 0.66 ms (6.2 MB/s, 86%), both
  md5-exact over every run today; the ~60–100 µs left per cycle is the two
  control-path legs a proxy cannot remove (header in, OKAY out: one packet
  time plus one hop each). First-run-after-idle cold watch: 2–5× slower,
  independent of the proxy.

- **M2 exit items (2026-10-10, 09:10–09:40).** `bm flash fw.uimg` puts the
  firmware at sector 0x18800; the SPL takes the Falcon path and the firmware
  is at its prompt 179 ms after the SPL's jump stamp, watch at 480 Mb/s,
  `adb devices` → `device`. Five warm reboots (`reboot` → watchdog → SPL →
  Falcon) all came back the same way. Software attach/detach (`hostcycle N`
  = unmount + re-enumerate): 30 cycles, 30 re-binds, 0 failures; one cycle
  fired 0.7 s into a 10 MB push: the transfer aborted, the watch re-bound
  230 ms later, no fault record. Bug found on the way: TinyUSB's
  `async_advance_isr` frees a removed QH but not the qTD still attached to
  it (the pending IN read), one leak per unplug until `qtd_find_free()`
  failed and every submit on the next device failed — fixed in the fork.
  Expected and still open: after a cycle the Mac's adb transport is stale
  (the new adbd waits for a CNXN the Mac will not resend) until the server
  restarts — exactly the halt pulse + CNXN replay of M4a. The cold power-on
  Falcon boot still needs a hand on the cable; the card holds this build.

- **M4a, the semantics (2026-10-10, 09:45–10:15, usbproxy-bm f5eea39 →
  4106fd4).** `bridge.cpp`'s host-stream framer, idle sink, CNXN
  capture/replay and IN-only halt pulse ported into `bridge.c` (one decision
  per gadget read; CNXN kept only if its checksum holds; replay at once if
  captured while idle, else 500 ms after the bind; halts cleared only when
  set). Two things the Linux version never had to know:
  - *macOS adb needs two STALLs.* It clears a stalled pipe host-side (IOKit
    `ClearPipeStall`, nothing on the bus) and retries once; only the retry
    failing drops the transport. TinyUSB's sunxi DCD cleared SEND_STALL in
    the SENTSTALL interrupt, so a halt stalled exactly one transaction and
    the pulse never kicked anything (`adb reboot` hung, `device` listed with
    no watch). Fixed: SENT_STALL is acknowledged, the halt persists until
    `dcd_edpt_clear_stall()`, which now also resets the data toggle (USB
    9.4.5, as Linux musb does).
  - Nothing else: the framer, replay and pulse behaved as on Linux,
    including the double kick (two CNXNs captured idle 1 s apart).
  Verified, all with one adb server left running: boot → `device` via
  replay; 5 software detach/attach cycles with `adb shell` after each;
  `adb reboot` returns in 0.1 s, `offline` within 1.6 s, `device` + shell
  49 s later (the watch's boot) via the captured CNXN; `adb reboot
  bootloader` → `fastboot devices` lists USBPROXYBM, the parked `getvar`
  (14 bytes) delivered at the fastboot bind, `getvar product` = catfish,
  `getvar all`, `fastboot reboot`, adb back 81 s later. The fastboot slot is
  never halted. Mac→watch cut-through is gated on an accepted message so it
  can never leak bytes the framer drops.

- **M4a, the rest of the trimmed list (10:15–10:45).** Progress-gated
  watchdog: with a watch bound, musb FRNUM (while the Mac keeps the bus
  awake) and EHCI FRINDEX are sampled every 250 ms; either stuck for 2 s
  stops the feed, logs, records a fault and the board resets 8 s later.
  `hangusb` (halts the EHCI) proved it: "usb progress lost (musb ok, ehci
  stuck)" at +2 s, reset, next boot's `bm check` says "ehci frame counter
  stuck", the boot after that is clean (the record is one-shot now). `bm
  check --adb` also requires `host_link=480` and the slot bound. Dropped by
  agreement: SPL GP-flag→FEL retarget and the 300 ms IN drain before
  cancelling at unbind. The card holds usbproxy-bm 23e5557 as the Falcon
  payload; U-Boot proper stays on it for the dev loop.
  **Where this leaves the project:** M-1 to M4a done. The firmware does
  everything DEVELOPMENT.md §7 asks of the persistent gadget on the bench,
  at 86–90% of direct throughput, booting in 0.18 s after the SPL. M4b
  (soak and fault injection, then the repaired appliance unit) is what is
  left before it can replace the Linux image.

- **M4b soak, first day (10:30–16:15), three findings.** `scripts/bm-soak.py`
  drives it (reboots / concurrent push+pull with a shell loop / fastboot
  cycles / idle watch; `all` chains them). 100 warm reboots passed (adb
  back in 0–3.6 s, `bm check --adb` clean each time). Then:
  1. **Concurrent push+pull corrupted the pull** (never the push, never
     sequential): about one 8 MB iteration in four came back with 64 wrong
     bytes, or adb dropped the transport with "failed to read copy
     response: EOF" when the damage hit a header. The 64 bytes were always
     a watch OKAY header (the push's ack) followed by 40 bytes of a payload
     two WRTEs older: the content of a 512-byte slot the TX FIFO had held
     before, with the first 16 words of the new packet missing. Ruled out
     one by one: the Mac (same data via the adb trace), the EHCI buffer (a
     canary in the last word of every 64-byte line never tripped in 10k+
     packets, so the overlay count does *not* run ahead of the data as M3
     believed), the cache (explicit invalidate changed nothing), the DPB
     early fill (same rate with it off: 6/15 vs 6/15). What changed the
     outcome: TX via the inner DMA, 12/12 clean; `cut 0`, clean (one
     4096-byte transfer per message, loads mostly from the TX ISR);
     **ep1 TX FIFO single-buffered, 20/20 clean** with throughput unchanged.
     So: PIO loads into the sunxi musb's double-buffered TX FIFO lose their
     first 16 words about once per 2000 packets while the RX side is busy.
     Linux keeps the sunxi FIFOs single-buffered; DPB had bought nothing in
     M3 anyway. Fixed in the fifo_map (usbproxy-bm c6f2f59), RX stays
     double-buffered. The M3 "64–128 stale bytes at packet starts" that
     motivated the one-packet cut-through lag was very likely this same
     bug; the lag stays as margin (~10 µs per message), `cutlag`/`cutcanary`
     knobs allow revisiting.
     **Second mechanism, same 64-byte signature** (the soak's shell loop on
     top of push+pull still corrupted 1 pull in 10 after the FIFO fix; 0 in
     20 without the loop): the stale bytes were a cache line's image of an
     older w2h buffer, read by the musb PIO from the core's cache instead
     of DRAM. The bridge buffers are cached and contiguous, and the PIO copy
     loop's last iteration speculatively loads one word past the piece it
     copies, so the first line of the *next* buffer lands in cache before
     the EHCI has written it; the submit-time invalidate cannot help with
     that. Fix: invalidate each cut-through piece right before it is handed
     to the DCD (its DMA is done by then), and invalidate musb RX DMA ranges
     after completion for the framer's sake. 30/30 shell-loop runs clean
     (usbproxy-bm 95c4116). M3's "64–128 stale bytes at packet
     starts" fits this one even better than the FIFO bug.
  2. **Mac sleep looped the board.** This Mac sleeps after 1 min idle; when
     it did, the gadget logged `suspended`, then `resumed` 2 ms later with
     no SOFs following, so `tud_suspended()` was false, the progress gate
     saw musb FRNUM frozen, the watchdog reset the board, the re-enumeration
     woke the Mac over USB (`wake reason: USB2_plug`), and the two cycled
     every 57 s (13 reboots, `boots=` in `status` counts them). Fix: the
     gate no longer treats a silent gadget bus as a fault, only the EHCI
     (our own host controller) can fail it; the bus idle/active transitions
     are logged. A device cannot tell a sleeping host from a dead link, and
     resetting the board is the one thing that makes it worse. Mac
     sleep/resume with the fix is a hands-on test still owed (the Mac must
     be woken by hand; nothing on the rig may do it).
  3. **The watch's cradle drops are power, not data and not firmware.** The
     watch left the EHCI port five times today (PORTSC 0x1000, nothing for
     minutes until a reseat). Its own `dmesg` shows the order each time:
     charger status interrupt, "USB input removed", *then* the controller's
     disconnect event, and `charger_online = 0` for the whole gap. The
     Zero's type-A VBUS is hard-wired on (DTS comment) and the board never
     rebooted, so 5 V vanished between the Zero's socket and the pogo pins.
     Drops happened while the watch was still drawing its 500 mA charge
     current; once it reached termination the link held. Worth a look at
     the cradle plug and pins before the appliance unit.
  4. **Fastboot through the cut-through path** (found with narwhal, 16:30):
     `getvar all` failed with e00002e8 on the Mac's 64-byte response read
     and stayed failed until a reseat. The fastboot slot's watch reads were
     whole-buffer (4096), so the watch→Mac cut-through applied to it and
     once forwarded a 512-byte piece, which the host's 64-byte read cannot
     take. The fastboot slot now reads one watch packet at a time
     (`want = wMaxPacketSize`, 512 here: a short reply still completes at
     once) and so never enters the cut-through path. 10/10 fastboot cycles
     after that (usbproxy-bm c0f05bb). Open: how 512 bytes "landed" on a reply
     endpoint at all; the progress lookup is by (daddr, ep) and both slots
     use 81/01 on these watches, so a lingering queue head from the device
     that just left is the suspect, not proven.
  **Narwhal vs catfish on the cradle question:** narwhal (charging at 85%,
  same Mac-powered Zero, same soak) came back from 16 of 16 reboots in
  25 s; catfish left the port after every reboot today. Whatever the
  supply margin, catfish or its cradle is the larger part of it.
  **Soak tally at the end of the day** (narwhal for everything after
  16:25, Mac held awake): reboots 100/100; concurrent push+pull with the
  shell loop 10/10 on the card build (plus 30/30 and 20/20 in the fix
  verifications); fastboot cycles 10/10 (bootloader in 0 s, 66 vars, adb
  back in 25 s each); idle 30 min, no link loss, no port change, `bm check
  --adb` PASS after it. Still owed: cold boots, unplug mid-push/mid-pull,
  Mac sleep/resume by hand, FEL recovery, the catfish `fastboot boot` of
  the AsteroidOS image (catfish needs a cradle that holds first), then the
  repaired appliance unit. The card carries usbproxy-bm 28b8b14 (see finding 5).
  5. **Cold power-on data abort (18:01, once).** Plugging the rig in with the
     watch cradled: the first boot died at 0.553 s, right after the Mac
     configured the gadget, in `dcache_invalidate_range(0x60190000, 32)`:
     a 32-byte invalidate (sizeof a qTD) on an address outside DRAM, i.e. a
     wild attached-qTD pointer in the EHCI poll; the watchdog's second boot
     was clean. Not reproduced yet. `.dma` was NOLOAD and never zeroed
     (ehci_init does clear its own pool, so this is hygiene rather than
     the proven cause); it is zeroed in start.S now. The fault record now
     also carries the interrupted mode's lr/sp and 32 stack words (verified
     with the console's `panic`), so the next one gives a backtrace via
     addr2line. Owed: the 20 cold boots of the hands-on list, with the UART
     captured. The card carries usbproxy-bm 28b8b14 (also: PA17 is the red
     status LED, PL10 the green power LED; names fixed).
  **Boot timing, measured (usbproxy-bm edfe146, narwhal cradled, 18:45):**
  warm reboot, gadget gone → re-enumerated by the Mac 0.31 s (3/3 runs;
  firmware `configured by the host` at 0.425 s after entry, the watch
  bound 0.05–0.45 s later), gone → `adb devices` says `device` 0.47–1.07 s
  (the spread is adb's 1 Hz IOKit rescan). Cold power-on from the 18:01
  capture: Mac port power → gadget enumerated 0.58 s (BROM+SPL ≈ 0.16 s
  of it). Linux on the same path (§0): warm reboot → gadget back
  1.13–1.16 s, cradle attach → adb 1.85 s (1.0 s settle debounce + the
  rescan); cold power-on was never measured there. So the plug-in →
  enumeration path is ~0.85 s shorter and the watch binds without the
  settle debounce; adb's own rescan now dominates what the Mac user sees.
  **14 cold power-ons with narwhal cradled (18:15–18:18, UART captured):**
  no fault, so the 18:01 abort is 1 in 15 so far. Every boot shows the
  same shape: gadget configured at 0.42–0.53 s; the watch's port connects
  and drops within 4 ms at 0.68 s (the watch re-asserting its gadget as
  VBUS returns), reconnects at ~1.8 s, bound at ~2.0 s. In four consecutive
  boots the Mac never configured the gadget: its log shows the old device
  object kept alive for a minute with transaction errors on EP0/EP1 every
  second and no "hardware connection lost", so it never saw the power
  cycle and never reset the new us. Fix: if the host has not configured
  the gadget 2 s after connect, the firmware drops the pull-up for 100 ms
  and presents itself again (`usb_represented=` in `status`; the console's
  `detach` suspends it). Three later boots also show `suspended`/`resumed`
  2 ms apart a few seconds after configuration: the Mac idling a device
  nobody has open yet and waking it as adb opens it, benign. Card:
  usbproxy-bm c77ece5.
  Also found: the Mac's own kernel panic at 15:03 (AppleCentauriManager,
  Apple's Wi‑Fi/Bluetooth coprocessor driver, "wait for termination failed
  … 65s") had nothing to do with the gadget, which sits on usb-drd0.
  Lesson for the soak tooling: a run that fails 100% after having failed
  20% is the rig, not the fix — check `boots=` and the Mac's power state
  before believing it.

## 9. Codex review (2026-10-09), triaged

Prompted with the whole document and read-only access to both repos; it
also web-checked the H3 datasheet, TinyUSB and CherryUSB sources. Verdict
on each point; "fixed" means the sections above now say so. The review
predates the decision to use a stock dev unit with a UART, which removes
its "inaccessible, repaired unit" caveat from the effort estimate but
changes nothing else.

| Codex finding | Verdict |
|---|---|
| TTBCR N=2 cannot map DRAM: 1 GiB is exactly where TTBR1 starts | Agree, fixed (N=0, 16 KiB) |
| HCI_ICR `0x701` is INCR8/INCR4/INCRX-align/ULPI bypass; INCR16 is bit 11 and stays clear | Agree, fixed |
| TinyUSB `ehci.c` is more than "add barriers": NXP speed bits and TT register, CONFIGFLAG not set, external H3 port proves only itself | Agree, fixed in §4 |
| TinyUSB runs one transfer per endpoint (one qTD); "chained qTDs"/queued packets need HCD changes | Agree, §5 datapath rewritten around it |
| Fastboot tail sizing is only unnecessary with packet-level gadget reads; a 4 KiB read stalls on a full-packet tail regardless of raw-gadget | Agree, fixed (per-packet reads are now a stated rule) |
| 1.3 ms RTT ↔ 1.3 MB/s mixes baselines: 1.3 ms predicts 3.15 MB/s; the current unaccelerated cycle is ~1 ms | Agree, numbers corrected throughout |
| Boot arithmetic double-counted the 0.19 s card read; retained SPL work is ~0.11 s; warm reboot ≠ cold power-on | Agree, fixed |
| GP1 guard only covers failure before the marker is cleared; WDT mode value is `0x71` in the patch | Agree, fixed; progress check added |
| "Hardware fault, stays" is more certain than the evidence (cold soak still owed) | Agree, softened; moot on the stock dev unit |
| 12 patches, not 13; "a few lines each" ignores enumeration/DMA/sequencing work | Agree, fixed |
| GIC IDs are SPI+32 (103/106), the sketch used DT indices | Agree, fixed |
| Direct RTT is a baseline, not the bridge's floor; push and pull need separate models | Agree, §2/§7 reworded |
| Legacy adb bounds one stream, not the transport; `4096/cycle` is an approximate ceiling | Agree, reworded |
| EHCI can do several bulk transactions per microframe; "one microframe per hop" is not a bound | Agree, reworded |
| **EHCI Interrupt Threshold Control defaults to 8 µframes = 1 ms on the H3; TinyUSB leaves it; must set 1** | Agree — the most useful single catch; in §2, §3, §5, M2 |
| PIO cost is the whole service (~17,600/s at 9 MB/s), not a 512 B copy | Agree, §2/§7/M1 |
| TinyUSB defers completions to the task; the 1 ms tick must not become the polling interval | Agree, §5 |
| M3 experiment design: paired runs, physical-counter stamps, distributions, a bulk responder to separate watch turnaround | Agree, adopted as M-1/M3 |
| Missing parity: idle OUT sink, IN drain before detach, DMA lifetime on detach, packet semantics, separate up/downstream state machines, enumeration details, CPU runtime work, recovery independent of CDC, log persistence design, transition tests | Agree on all; §1, §3, §5, §7, M0/M4b |
| CherryUSB + RTOS should stay a serious alternative; an RTOS is not a millisecond penalty | Agree, §4 reworded |
| OHCI later is stack-level work, not a driver drop-in | Agree, §3 |
| 4–8 weeks is optimistic; 8–12+ for parity; split M4; M0 needs the runtime skeleton and failure tests; M1 needs a bulk benchmark; M2 needs transfers and cancellation; prove recovery before M0 | Agree, §8 rewritten |
| Measure the current Linux path without acceleration first | Agree — it is now M-1 and the first thing to do |
| Single-buffered FIFOs first; timed retry state machine; pin stack commits; "no ACK spoofing" rather than "no adb parsing"; SRAM-only does not require MMU off | Agree, all fixed |
| "Do not use the identical raw+FAT payload workflow for experiments" | Moot: DFU, U-Boot proper and the FAT partition are out of scope now |

Codex's verdict, which I share: a bounded spike, not a committed
replacement; boot and image-size savings do not justify owning both USB
stacks and all recovery behaviour. The deciding measurement is the mean
end-to-end 4096-byte WRTE→OKAY cycle on the real watch with acceleration
off, paired direct vs prototype: approaching 0.5 ms → proceed toward
8 MB/s parity; ~0.8–1 ms → stop.

## Sources

- TinyUSB: [PR #1220, F1C100s DCD](https://github.com/hathach/tinyusb/pull/1220),
  [`dcd_sunxi_musb.c`](https://github.com/hathach/tinyusb/blob/master/src/portable/sunxi/dcd_sunxi_musb.c),
  [`ehci.c`](https://github.com/hathach/tinyusb/blob/master/src/portable/ehci/ehci.c),
  [`usbh.c`](https://github.com/hathach/tinyusb/blob/master/src/host/usbh.c),
  [0.14.0 release notes (tuh_edpt_xfer)](https://github.com/hathach/tinyusb/releases),
  [discussion #1599 (EHCI portability)](https://github.com/hathach/tinyusb/discussions/1599)
- CherryUSB: [`port/musb/usb_dc_musb.c`](https://github.com/cherry-embedded/CherryUSB/blob/master/port/musb/usb_dc_musb.c),
  [`port/musb/usb_glue_sunxi.c`](https://github.com/cherry-embedded/CherryUSB/blob/master/port/musb/usb_glue_sunxi.c),
  [`port/ehci/usb_glue_t113.c`](https://github.com/cherry-embedded/CherryUSB/blob/master/port/ehci/usb_glue_t113.c),
  [`class/hub/usbh_hub.c`](https://github.com/cherry-embedded/CherryUSB/blob/master/class/hub/usbh_hub.c),
  [`osal/`](https://github.com/cherry-embedded/CherryUSB/tree/master/osal)
- Allwinner H3 datasheet v1.2: [EHCI USBCMD / interrupt threshold p. 493, HCI_ICR p. 521](https://cdn.hackaday.io/files/1872678012031328/Allwinner_H3_Datasheet_V1.2.pdf)
- EHCI spec §§4.8–4.9: [Intel EHCI specification](https://www.intel.com/content/dam/www/public/us/en/documents/technical-specifications/ehci-specification-for-usb.pdf)
- ARM short-descriptor TTBR0/TTBR1 selection: [ARM DDI 0406C](https://support.arm.com/documentation/ddi0406/c/)
- adb: [legacy protocol](https://android.googlesource.com/platform/system/core/+/de980f27a701eb8f7130218f9bac768fe7155d30/adb/protocol.txt),
  [zero-length packets and USB completion](https://android.googlesource.com/platform/packages/modules/adb/+/HEAD/docs/dev/zero_length_packet.md)
- H3 bare metal: [uli/allwinner-bare-metal](https://github.com/uli/allwinner-bare-metal) (`jh` branch `usb.c`),
  [mprymek/allwinner-baremetal](https://github.com/mprymek/allwinner-baremetal),
  [trebisky/orangepi](https://github.com/trebisky/orangepi)
- Memory map: [QEMU `hw/arm/allwinner-h3.c`](https://github.com/qemu/qemu/blob/master/hw/arm/allwinner-h3.c),
  [linux-sunxi BROM](https://linux-sunxi.org/BROM)
- FEL re-entry from the SPL: [U-Boot `rbrom` patch (SUSE)](https://sources.suse.com/SUSE:SLFO:Products:SLES:16.0/000productcompose/0b9fffee43a59e7b8a19200fe73bb1bf/INCLUDED/SUSE:SLFO:1.2::u-boot::ce5513236e72d322942ccc1396c2a207/0016-cmd-boot-add-brom-cmd-to-reboot-to-.patch),
  [sunxi: Normalise FEL support](https://lists.denx.de/pipermail/u-boot/2015-February/204447.html)
- Zephyr: [Orange Pi Zero board](https://docs.zephyrproject.org/latest/boards/xunlong/opi_zero/doc/index.html)
- Local: `drivers/usb/musb/sunxi.c`, `drivers/phy/allwinner/phy-sun4i-usb.c`,
  `drivers/clk/sunxi-ng/ccu-sun8i-h3.c`, `arch/arm/boot/dts/allwinner/sunxi-h3-h5.dtsi`
  in the 6.6.85 kernel-source of the OrbStack build; DEVELOPMENT.md §8–§10; MUSB-BULK-OUT.md.
