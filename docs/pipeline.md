# Detection & solve pipeline

How a camera frame becomes a pointing solution, and how the three
detection-related controls (**Seeing**, **Star detection**, **Sky background**)
map onto the underlying settings.

The single most important branch is `extractor_backend`:

- **`tetra3` (the "Legacy" preset)** — an exact re-creation of AstroKeith's
  `eFinder_cli` extraction + solve (olive-solve's
  `get_centroids_from_image_fast` + its solver). It **bypasses** sycamore and
  the temporal background cache entirely.
- **`sycamore` (the "Good"/"Light-pollution" presets)** — the matched-filter
  extractor routed through `bg_cache`, with the temporal cache, hot-pixel
  repair, and trail rejection.

> **Scope note:** "Legacy = AstroKeith" applies to the *extraction + solver*.
> It still runs inside diofinder's harness — auto-exposure, calibrated FOV
> tolerance, and the IMU attitude hint wrap both backends.

---

## 1. Runtime pipeline (branches on `extractor_backend`)

```mermaid
flowchart TD
    F["Camera frame · 8-bit · shared memory"] --> B{"extractor_backend ?"}

    %% ---- Legacy / tetra3 ----
    B -->|"tetra3  ·  LEGACY"| T["olive-solve<br/>get_centroids_from_image_fast"]
    T --> Tn["local_mean background — hardcoded<br/>filtsize = detect_uniform_filter_size · 25<br/>noise = global_root_square ← detect_noise_mode<br/>sigma = detect_sigma · 2.0<br/>min_area 5 · max_area 100 · downsample 1<br/>—<br/>NO matched filter · NO temporal cache<br/>NO hot-pixel repair · NO trail rejection"]
    Tn --> C

    %% ---- Sycamore / Good · Bad ----
    B -->|"sycamore  ·  GOOD / BAD"| G["bg_cache.detect(...)"]
    G --> S{"cache STEADY ?"}
    S -->|"yes"| SC["detect_stars_with_cache<br/>√N temporal model + free hot-pixel reject"]
    S -->|"no · slew / warm-up"| SP["detect_stars · per-frame"]
    SC --> Sn
    SP --> Sn
    Sn["matched filter · detect_kernel_sigma<br/>bg_mode · row / block / line / uniform / column …<br/>noise_mode · mad / global_rms<br/>max_axis_ratio · trail rejection<br/>hot-pixel repair"] --> C

    %% ---- shared tail ----
    C["cap to max_solve_stars · 50<br/>sycamore = brightest-first"] --> SV["olive-solve solve_from_centroids<br/>fov_estimate + fov_max_error · calibrated / loose<br/>match_radius · match_threshold · solve_timeout<br/>IMU attitude hint · if calibrated"]
    SV --> R["RA / Dec / roll / matches"]
```

---

## 2. Controls → settings (Seeing is the master; the rest are overrides)

```mermaid
flowchart TD
    SEE["SEEING preset · Good / Light-pollution / Legacy<br/>MASTER — writes all 16 keys at once"]

    SEE -->|"Legacy"| LEG["extractor_backend = tetra3<br/>detect_sigma = 2.0<br/>detect_noise_mode = global_rms — honored<br/>detect_uniform_filter_size = 25 — honored<br/>detect_bg_mode = uniform_mean — inert under tetra3<br/>detect_kernel_sigma = 1.5 — inert under tetra3<br/>detect_max_axis_ratio = 0.0 — inert under tetra3<br/>+ match / auto-exposure / star_db keys"]

    SEE -->|"Good / Light-pollution"| GB["extractor_backend = sycamore<br/>all detection keys active"]

    subgraph OV["fine-grained overrides — show as drift → Customized badge"]
      O1["Star detector → extractor_backend · tetra3 ⇄ sycamore  ◄ THE tweak"]
      O2["Detection sensitivity → detect_sigma"]
      O3["Sky background → detect_bg_mode + size<br/>only bites when backend = sycamore"]
    end

    LEG -.->|"flip ONLY the detector"| O1
    O1 -.->|"now sycamore: inert keys wake up"| GB
```

Starting from **Legacy** and flipping **only** the Star detector to Sycamore
keeps `detect_sigma=2`, `detect_bg_mode=uniform_mean`,
`detect_noise_mode=global_rms`, `filtsize=25` — but now they run through
sycamore, so `uniform_mean`+`global_rms` is honored (the deliberate
"tetra3-reference pipeline reproduced in sycamore"), **plus** the matched-filter
gate, the temporal cache, hot-pixel repair, and trail rejection all switch on.

---

## 3. Which detection keys are honored under each backend

| Key | `tetra3` (Legacy) | `sycamore` (Good / Bad) |
|---|---|---|
| `detect_sigma` | ✅ threshold | ✅ threshold |
| `detect_noise_mode` | ✅ `global_rms` → `global_root_square` | ✅ |
| `detect_uniform_filter_size` | ✅ as `filtsize` | ✅ only when `bg_mode = uniform_mean` |
| `detect_bg_mode` | ❌ ignored (`local_mean` hardcoded) | ✅ |
| `detect_kernel_sigma` | ❌ no matched filter | ✅ |
| `detect_max_axis_ratio` | ❌ no trail rejection | ✅ |
| `detect_local_noise` | ❌ | ✅ |
| temporal `bg_cache` (√N, hot-pixel) | ❌ bypassed | ✅ |
| `max_solve_stars` | ✅ | ✅ |
| `match_radius` / `match_threshold` / fov / `solve_timeout` | ✅ (solver) | ✅ (solver) |

So **Legacy** = `local_mean` background · 25-px window · global-RMS noise ·
sigma 2 · no temporal cache · no trail rejection. The `uniform_mean` /
`kernel_sigma` / `max_axis_ratio` values it writes are inert under tetra3 — they
exist so that toggling back to Good/Bad (or just flipping the detector to
Sycamore) re-tunes cleanly.
