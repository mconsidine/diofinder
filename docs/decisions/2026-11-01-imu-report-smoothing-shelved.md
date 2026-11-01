# Reported-pointing jumpiness — shelved (2026-11-01)

**Status: SHELVED, not abandoned.** The problem is real and still unfixed in
the 0.11.75 candidate. The quick 0.11.71 deadband patch is rejected; when this
is picked up again, implement the proper fix below.

## The problem

The SkySafari crosshair (`:GR/:GD`, and since 0.11.72 the `:GA/:GZ` Alt/Az,
which derive from the same `_report_radec`) looks **jumpy while the scope is
moving**. Two hard snaps in the comms report path cause it:

1. **Solves landing normally.** The solve-freshness gate (P1,
   `imu_predict_hold_solve_s`) makes `_imu_predict` return None, so the report
   is the latest *raw* solve. The alpha-beta filter never runs. Motion-blurred
   frames each solve slightly differently, and the crosshair hops between them.
2. **Solve drought.** The IMU prediction is smoothed by
   `_imu_predict_smoothed`, but that filter clears on every `imu_ref` `ref_t`
   change, so the crosshair snaps whenever a solve returns.

Both are the same design flaw: a new solve **replaces** the reported position
instead of being **blended into** it.

A likely contributor to the size of snap 2 (and to bias in the anchor
generally): the IMU quaternion paired with each solve (`imu_ref`, the Kabsch
pairs, the solve hint) is sampled from the solver's per-frame `shared_cfg`
snapshot, **not at mid-exposure**. The gap is about half the exposure plus
ISP/readout time, roughly 0.15–0.6 s, so every re-anchor during a slew lands
offset by roughly slew rate × gap.

## The rejected quick fix (v0.11.71)

Branch `claude/imu-report-deadband-0.11.71`, commit `3ff2df5`, preserved by the
tag **`archive/imu-report-deadband-0.11.71`** so the branch can be deleted.

It added `imu_report_deadband_deg` (default 0.25°): position hysteresis on the
final reported RA/Dec in `_report_radec`. The readout holds until the candidate
moves more than the deadband, then jumps to it.

Why it is not merged:

- **The readout can stay stuck up to 0.25° off after the scope stops.** With
  no settle/convergence, the held position never returns to the true solve
  unless the error exceeds the band. That's about 18 px at 51 arcsec/px,
  for as long as the scope sits still.
- **It turns smooth motion into 0.25° steps** during a slew. That is a
  different kind of jumpiness, not smoothness.
- **It treats the symptom.** Neither snap is removed; sub-band changes are
  hidden.

## The proper fix (do this when resuming)

### 1. One continuously propagated, gently corrected reported attitude

Keep a single reported attitude state in comms, updated two ways:

- **Propagate** every IMU sample (or on each poll) by the IMU delta, conjugated
  through `imu_frame_R`. This is the existing exact quaternion path in
  `_imu_predict`, with no hold gate. This carries real motion with no lag.
- **Correct** on every new solve by pulling the state a fraction of the way
  toward the solve (a complementary filter / small-gain attitude correction,
  e.g. slerp with gain k≈0.2–0.5, or a gain scaled by solve quality
  (matches, RMSE)). Large disagreements (> a few × the expected error, e.g.
  after an unsensed slew or a bad fit) snap immediately.

Then the report is always the filtered state: no hard re-anchor, so P1's
"report the raw solve" and the alpha-beta reset on `ref_t` both go away.
Stationary, the state converges to the solve average (no stuck offset, the
deadband's flaw). Moving, the IMU carries it and solves only trim drift.

Fallbacks to keep: no IMU / no `imu_frame_R` fit / `imu_predict_enabled=false`
→ report solves directly (today's behaviour); keep the `imu_exact_predict`
kill switch.

### 2. Mid-exposure IMU timing

`frame_meta` (v0.11.46) already carries the exact `SensorTimestamp` and
`ExposureTime` per frame seq. Keep a ~2 s ring of `(q, t)` IMU samples in the
solver (or publish it from the IMU thread), and interpolate (slerp) to the
exposure midpoint of the solved frame. Use that quaternion for `imu_ref`,
the calibration/Kabsch pairs, and the solve hint. Expect it also to tighten
the Kabsch fit and reduce re-anchor error during slews.

### 3. Validation

- Unit: a simulated slew + noisy solves + IMU jitter. Assert no step larger
  than X at re-anchor, bounded lag while moving, and convergence to the solve
  mean within N s when stationary (the regression the deadband would fail).
- On-sky A/B with the current path behind a live toggle (`solver_params_set`),
  like the other IMU knobs, judged in SkySafari during slews and at rest.

## Pointers

- Report path: `diofinder/comms_proc.py` `_report_radec`, `_imu_predict`,
  `_imu_predict_smoothed`, `_report_altaz`.
- Anti-oscillation layers P1/P2/P3: CLAUDE.md "Anti-oscillation".
- Reference timing: `diofinder/frame_meta.py`; IMU publish: `imu_proc.py`.
