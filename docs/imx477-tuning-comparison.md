# IMX477 libcamera tuning — `imx477_finder.json` comparison

Reference comparison of the shipped finder tuning against the two stock
Raspberry Pi vc4 (bcm2835) tunings. Generated from
`tuning/imx477_finder.json` vs. the upstream
`raspberrypi/libcamera` `imx477.json` and `imx477_scientific.json`
(`src/ipa/rpi/vc4/data/`).

## Provenance

`imx477_finder.json` is **derived from `imx477_scientific.json`** (commit
`212db2f`, PR #61), not from the standard `imx477.json`. It is the scientific
tuning with **exactly two edits**: `rpi.dpc` disabled and a steeper asinh
`rpi.contrast` gamma curve. Everything else is byte-identical to scientific.

All three are `version 2.0`, `target bcm2835`.

## Stage-by-stage

| Stage | standard `imx477.json` | `imx477_scientific.json` | `imx477_finder.json` | finder source |
|-------|------------------------|--------------------------|----------------------|---------------|
| `rpi.black_level` | `black_level: 4096` | same | same | = both |
| `rpi.dpc` | `{}` (default ≈ on) | `{}` (default ≈ on) | **`{"strength": 0}`** | **EDIT** |
| `rpi.lux` | std | sci | sci | = scientific |
| `rpi.noise` | std | sci | sci | = both* |
| `rpi.geq` | `offset 204, slope 0.01078` | same | same | = both |
| `rpi.sdn` | std | sci | sci | = both* |
| `rpi.awb` | std tables | sci tables | sci tables | = scientific |
| `rpi.agc` | std | sci | sci | = scientific |
| `rpi.contrast` | std gamma | sci asinh gamma | **steeper asinh gamma** | **EDIT** |
| `rpi.ccm` | std matrices | sci matrices | sci matrices | = scientific |
| `rpi.sharpen` | `0.75/0.5/1.0` | same | same | = both |
| `rpi.sync` | std | sci | sci | = both |
| `rpi.alsc` | **present** | *absent* | *absent* | dropped (via scientific) |
| `rpi.hdr` | **present** | *absent* | *absent* | dropped (via scientific) |
| `rpi.nn.awb` | **present** | *absent* | *absent* | dropped (via scientific) |

\* identical value across scientific and standard, so non-discriminating.

`standard` carries **15** algorithms; `scientific` and `finder` carry **12**
(they drop `alsc`, `hdr`, `nn.awb`).

## The two edits (the entire finder-vs-scientific diff)

### 1. `rpi.dpc` — defective-pixel correction OFF
```
scientific / standard:  {}                  → default strength (correction on)
finder:                 {"strength": 0}     → off
```
DPC's signature for a "defect" is a single bright pixel against a dark
neighborhood — **identical to a faint star**. Left on, it deletes 1–2 px faint
stars. There is **no libcamera runtime control** to disable DPC, so it must be
turned off *in the tuning file*.

### 2. `rpi.contrast` — steeper asinh companding gamma
`ce_enable: 0` in all three. The finder gamma curve lifts the faint end harder
than either stock curve, so faint stars survive the 12→8-bit reduction with
resolvable codes instead of landing in 0–2 DN quantization mud. Output code for
a given input (16-bit grid):

| input | standard | scientific | **finder** |
|------:|---------:|-----------:|-----------:|
| 1024 | 5040 | 4608 | **7582** |
| 2048 | 9338 | 8401 | **13888** |
| 4096 | 15312 | 13922 | **22747** |
| 8192 | 25744 | 21488 | **33006** |

finder > standard > scientific in the shadows; the asinh only gives up
resolution near saturation (bright end), which detection doesn't use. Gamma,
like DPC, has **no runtime control** — it always applies on the output path — so
it too lives in the tuning file. `black_level` is kept at the 4096 sensor
pedestal.

## Why everything else is left unchanged

The finder pipeline reads the **Y (luma)** plane of a `YUV420` stream with these
runtime controls applied in `camera_proc.py` (`_init_camera`, lines ~191–200):

```python
controls = {
    "AeEnable":           False,   # manual exposure/gain (auto-exposure controller drives it)
    "AwbEnable":          False,   # no auto white balance
    "NoiseReductionMode": 0,       # denoise off
    "Sharpness":          0.0,     # sharpening off
    "Saturation":         0.0,     # chroma zeroed → effectively mono
    "ExposureTime": ..., "AnalogueGain": ..., "FrameDurationLimits": ...,
}
```

So each stock stage falls into one of four buckets, and **none of them needs a
tuning edit**:

| Bucket | Stages | Why no edit needed |
|--------|--------|--------------------|
| **Disabled at runtime by a control** | `rpi.agc` (AeEnable=False), `rpi.awb` (AwbEnable=False), `rpi.sdn`/`rpi.noise` denoise (NoiseReductionMode=0), `rpi.sharpen` (Sharpness=0), `rpi.ccm` (Saturation=0 zeros chroma; luma is unchanged by a color-matrix) | The control neutralizes the stage regardless of its tuning values — editing the tuning would be redundant. This is why their scientific-vs-standard value differences are **moot**: inert either way. |
| **Inert / informational** | `rpi.lux` (feeds the now-off AGC/AWB), `rpi.sync` (frame-timing, not pixels) | Produces no pixel effect once AE/AWB are off. |
| **Benign sensor corrections, identical scientific=standard** | `rpi.black_level` (pedestal subtraction — *must* stay), `rpi.geq` (Bayer green-imbalance, a small raw-domain correction) | Not detection-hostile; removing them would hurt, not help. Same value in every tuning. |
| **No runtime control AND touches detection luma** | `rpi.dpc`, `rpi.contrast` (gamma) | **The only two that must be edited in the file** — hence the two edits above. |

### Relative to standard `imx477.json` specifically
Beyond the two edits, finder differs from standard only by inheriting
scientific's `awb`/`agc`/`ccm` values and by **dropping** `alsc`, `hdr`,
`nn.awb`. None needs reconciling:

- **`rpi.alsc`** (lens-shading / vignetting correction) — dropped by scientific.
  Uncorrected vignetting is a smooth radial background, which diofinder removes
  downstream in `bg_cache` (per-row / block background modes). Leaving it out
  keeps the data uniform and avoids ALSC's per-channel shading gains perturbing
  the luma. No need to re-add.
- **`rpi.hdr`** — single-exposure finder; not applicable.
- **`rpi.nn.awb`** — a white-balance estimator; AWB is disabled at runtime.
- **`awb` / `agc` / `ccm` value differences** — all three stages are disabled or
  chroma-zeroed at runtime, so scientific's vs standard's values are
  indistinguishable in the finder's luma output.

## Reproduce on-device
```bash
diff <(jq -S . /usr/share/libcamera/ipa/rpi/vc4/imx477_scientific.json) \
     <(jq -S . /usr/share/libcamera/ipa/rpi/vc4/imx477_finder.json)
# → only rpi.dpc strength and the rpi.contrast gamma_curve differ.
```
Switch profiles live with `diofinder-ctl` / the Camera page `tuning_set`
(`finder` | `scientific` | `standard`; restart required).
