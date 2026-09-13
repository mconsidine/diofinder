# 2026-09-13 — evaluated non-Pi camera platforms; decision: stay on Raspberry Pi

**Date:** 2026-09-13
**Session URL:** https://claude.ai/code/session_01SrP35Ht2NZf6v9HDtzsMmH
**Repo:** mconsidine/diofinder (branch `olive`)
**Outcome:** doc-only. No code, no platform change. Recorded so this isn't relitigated.

## Context / trigger

Raspberry Pi Zero 2W boards were unavailable at the time. That prompted a survey
of alternative single-board computers + cameras that could host diofinder. The
survey concluded that **no alternative is worth the porting cost**, and that the
shortage is best treated as a **sourcing problem, not an architecture problem.**

The load-bearing realization: the Pi's value was never the CPU — it was a rare,
fully-integrated imaging stack (**CSI + a tunable ISP + libcamera + picamera2 +
a first-class IMX477 driver + our uploadable tuning file**), all turnkey. Every
non-Pi path breaks a different link in that chain.

## Options evaluated (and why each was rejected)

### Orange Pi Zero 2W (Allwinner H618) — the original candidate
- **No CSI camera input at all.** The 24-pin expansion connector that sits where
  the Pi's CSI would be carries 2× USB 2.0, 100M Ethernet, analog A/V, IR, and
  button pins — **no CSI lanes.** Orange Pi's own answer is "use USB."
- Even setting that aside, H618 mainline camera support is raw-capture-only
  (`sun6i-csi` via libcamera's generic "simple" pipeline handler, no ISP), and
  **picamera2 is Raspberry-Pi-specific** so `camera_proc.py` wouldn't run
  regardless. Verdict: camera only over USB → see below.

### Arducam B0278 (CSI→USB UVC adapter for the existing IMX477) — rejected
- ✅ Genuinely UVC/plug-and-play (qv4l2/V4L2 see it; no SDK).
- ❌ **MJPG-only output.** Lossy JPEG destroys/mimics faint 1–3 px stars, and a
  fixed non-tunable webcam ISP runs before compression.
- ❌ **USB 2.0**, and the low-res convenience modes are 16:9 **crops** (changed
  FOV); only the heavy 4:3 MJPG modes preserve the field.
- ⚠️ Manual exposure exists but is **capped at 0.5 s** (`exposure_absolute`
  1–5000 @ 0.1 ms) and widely reported as flaky; **manual gain not reliably
  exposed.** No per-frame sensor metadata.
- Verdict: engineered for machine-vision/webcam use, hostile to faint-star
  plate solving.

### "Is 12 MP even needed?" — NO (this reframed the whole search)
diofinder never uses 12 MP: it downsamples to 960×760 (0.73 MP) and detects at
`bin=2` (~480×380). The 12 MP exists only to get the full-array FOV via ISP
downscaling; resolution is deliberately thrown away. Real requirements: ~10–20°
FOV, ~1 MP of working pixels, sensitivity to mag ≤ 8, manual exposure+gain, a
long-enough exposure ceiling, clean/linear frames. → **~1–2 MP mono is a better
match**, and going low-res keeps a frame **uncompressed** inside USB 2.0
bandwidth (the exact trap the B0278 falls into). Mono is a further win: ~2×
sensitivity, no debayer, cleaner PSF, no tuning file needed.

### The uploaded gamma curve — would it be lost?
On the Pi the custom `rpi.contrast` gamma (asinh companding for faint-star
survival across the 12→8-bit reduction) ran in the **VideoCore ISP hardware**.
Finding: none of the USB options has a Pi-class uploadable-LUT ISP, so the
*hardware feature* is lost — **but the *effect* is not.** The u8 reduction is
mandatory anyway (sycamore is u8-only), so reproducing the companding is a
single `uint8` LUT (`out = lut[frame]`, sub-ms) — **≈zero net cost** vs. a linear
reduction, *provided* you capture ≥10-bit RAW and own the reduction. On an
already-8-bit path (Y8/YUY2/MJPG) it's gone with no cheap recovery.

### USB camera classes (if forced onto USB)
- **Mono UVC machine-vision (e.g. Arducam OV9281 USB):** uncompressed YUY2 +
  V4L2 manual exposure, no SDK — but the **USB/UVC bridge is 8-bit only.** The
  OV9281 *sensor* does RAW10; the *USB webcam* does not pass it (RAW10 is a CSI
  property). So the companding lever is lost on this path.
- **Astronomy USB (ZWO ASI mono mini):** 16-bit RAW, unlimited exposure, best
  sensitivity/manual control — but **not UVC**; needs the ASI SDK/INDI
  (aarch64 supported). The only USB route that keeps ≥10-bit.

### Radxa Zero 3W (RK3566) + OV9281 **MIPI** — the "right" architecture, still rejected
This is the only alternative that preserves the correct pipeline: Pi-Zero form
factor, a real MIPI CSI connector, mono global-shutter **RAW10** (`Y10`/`Y10P`
via the `rkcif` raw path, ISP bypassed), and CPU parity (quad A55, so the
`sched_setaffinity` 1-3 / CPU-0 model carries over). It was spec'd as a
platform + capture-layer port (not a solver rewrite), ~3–6 weeks, gated on:
- **Driver bring-up (dominant risk):** OV9281 is **not supported on stock Radxa
  OS** (official image supports only RPi OV5647/IMX219); needs a custom kernel
  build + sensor driver + `.dtbo` overlay. Not turnkey.
- Exposure ceiling (VTS-limited) and on-sky sensitivity: unproven.
- A **second OS-image pipeline** (Radxa Bookworm/kernel 6.1 — **not** the
  Debian 13 "Trixie" originally hoped for), plus bus/UART/USB-gadget/WiFi-AP
  remaps (the Zero 3W's AIC8800-class WiFi AP mode is a real unknown).
- Full optical recalibration (choose a ~16 mm M12 lens to hold ~13.5° FOV so the
  existing `diofinder_13deg` solver DB still applies).

**Verdict:** technically sound but 3–6 weeks of front-loaded risk to escape a
supply problem. Not worth it.

## Decision

**Do not pursue a non-Pi hardware platform. Stay on Raspberry Pi and treat the
Zero 2W shortage as sourcing, not engineering.**

Rationale: replicating the Pi's clean imaging path anywhere else costs more than
the shortage does. The difficulty is not producing pixels — it's reproducing
CSI + tunable ISP + libcamera + picamera2 + IMX477 + tuning without a
multi-week port and recalibration.

**Escape hatch for "need hardware now":** the code is **not Zero-2W-specific** —
its only board assumption is "quad-core, pin solver to CPUs 1-3," which holds on
the **Pi 4, Pi 5, and CM4/CM5** too. Moving to any other Pi model is a
**zero-code, zero-recalibration** swap: IMX477 + libcamera + the tuning file all
come along unchanged. Pi 5 just makes the solver faster; CM4/CM5 on a carrier
keeps the compact form factor. Those are often in stock when the Zero 2W isn't.

## What would reopen this

- A **prolonged, ecosystem-wide** Raspberry Pi shortage (not just the Zero 2W).
- A concrete need the Pi can't meet (e.g. a much larger FOV or a cooled sensor).

If reopened, the **Radxa Zero 3W + OV9281 MIPI** path is the pre-vetted
front-runner, and the port was already spec'd (backend abstraction keeping the
Pi path byte-for-byte; V4L2 `Y10P` capture; asinh-companding LUT; V4L2
`frame_meta`; ~16 mm lens to reuse the DB; a Radxa image pipeline). Start with a
standalone **driver bring-up spike** as the GO/NO-GO gate before any other work.

## Sources (external, retrieved 2026-09-13)

- Orange Pi Zero 2W: no CSI lanes on the 24-pin expansion (Hackster launch
  coverage; Orange Pi product page).
- Arducam B0278 specs (welectron/CPC/UCTRONICS mirrors; Arducam quick-start +
  UVC wiki; forum threads on the 0.5 s exposure cap and flaky manual controls).
- Arducam OV9281: MIPI datasheet (8/10-bit RAW); USB board (UB0232) UVC
  YUY2/MJPG; ZWO ASI aarch64 SDK / INDI support.
- Radxa Zero 3W: RK3566, MIPI CSI, 40-pin GPIO; official Debian 12 Bookworm /
  kernel 6.1 images; OV9281 not in stock OS (Radxa forum "Support for OV9281");
  `veyeimaging/rk35xx_radxa` driver repo; device-tree-overlay wiki.
