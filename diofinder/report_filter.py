"""Smooth reported pointing: blend each new solve in instead of snapping to it.

Opt-in (``imu_report_smoothing``, default OFF) fix for the jumpy SkySafari
crosshair while the scope moves; design in
``docs/decisions/2026-11-01-imu-report-smoothing-shelved.md``.

The comms report path picks a TARGET each poll: the IMU-propagated pointing
from the latest solve (``_imu_predict`` without the solve-freshness hold) while
the scope moves, else the solve itself. Today the report IS the target, so it
hard-jumps whenever a solve lands or the source switches. Here the report is
the target plus an OFFSET (a small sky rotation):

* IMU source, new solve: the offset becomes ``(1 - gain)`` of the gap between
  the report (carried forward to now) and the new target, so each solve pulls
  the report a fraction of the way; the IMU carries the motion and solves only
  trim it. Each solve also corrects a learned DRIFT RATE of the offset
  (``rate_gain``; an alpha-beta / PI correction): a consistent IMU error such
  as a scale error in the IMU-to-camera fit would otherwise leave a steady
  lead or lag of (1 - gain) / gain times the per-solve error. The drift is
  clamped to a fraction of the slew rate and cleared when the scope stops.
  A gap over ``snap_deg`` (unsensed slew, bad fit) snaps.
* solve source (parked, or no IMU), new solve: a gap up to
  ``solve_jitter_deg`` is solve-to-solve noise and is averaged the same way
  (an exponential mean of the solves); a bigger gap is real motion the IMU
  did not report, so the report follows at once (no lag on a pan without IMU).
  In this source the offset also fades with time constant ``solve_decay_s``,
  so the report always settles exactly on the solves (no stuck offset).
* source switch (IMU <-> solve): the offset absorbs the whole gap
  (continuity); then it is trimmed / fades as above.
* no poll for ``reset_gap_s``: start over.

Also here: ``interp_quat`` + ``mid_exposure_monotonic`` for pairing a solve
with the IMU attitude at the MIDDLE of its exposure (the IMU thread publishes a
short sample history while the switch is on), instead of the attitude when the
solver happened to read it (0.15-0.6 s later; during a slew every re-anchor
was off by rate x that gap).

Pure (math only): unit-tested without hardware in tests/test_report_filter.py.
"""
from __future__ import annotations

import math

DEFAULT_GAIN = 0.3
DEFAULT_RATE_GAIN = 0.3             # beta: how fast the IMU-drift estimate learns
DRIFT_MAX_FRAC = 0.3                # learned drift <= this x the slew rate (+ floor)
DRIFT_FLOOR_DEG_S = 0.02
DEFAULT_SNAP_DEG = 1.0
DEFAULT_SOLVE_JITTER_DEG = 0.05     # ~3.5 px: bigger solve-to-solve steps are motion
DEFAULT_SOLVE_DECAY_S = 0.7
DEFAULT_RESET_GAP_S = 10.0


# --------------------------------------------------------------------------- #
# Small vector helpers (unit vectors on the celestial sphere)
# --------------------------------------------------------------------------- #

def radec_to_vec(ra_deg, dec_deg):
    ra, dec = math.radians(ra_deg), math.radians(dec_deg)
    c = math.cos(dec)
    return (c * math.cos(ra), c * math.sin(ra), math.sin(dec))


def vec_to_radec(v):
    x, y, z = v
    n = math.sqrt(x * x + y * y + z * z) or 1.0
    return (math.degrees(math.atan2(y, x)) % 360.0,
            math.degrees(math.asin(max(-1.0, min(1.0, z / n)))))


def _cross(a, b):
    return (a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2],
            a[0] * b[1] - a[1] * b[0])


def _dot(a, b):
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def rotvec_between(a, b):
    """Rotation vector (radians) turning unit vector a onto b (shortest way)."""
    c = _cross(a, b)
    s = math.sqrt(_dot(c, c))
    ang = math.atan2(s, max(-1.0, min(1.0, _dot(a, b))))
    if s < 1e-15:
        return (0.0, 0.0, 0.0)
    k = ang / s
    return (c[0] * k, c[1] * k, c[2] * k)


def rotate(v, r):
    """Rodrigues: rotate vector v by rotation vector r (radians)."""
    ang = math.sqrt(_dot(r, r))
    if ang < 1e-15:
        return v
    k = (r[0] / ang, r[1] / ang, r[2] / ang)
    c, s = math.cos(ang), math.sin(ang)
    kxv = _cross(k, v)
    kdv = _dot(k, v)
    return tuple(v[i] * c + kxv[i] * s + k[i] * kdv * (1 - c) for i in range(3))


def sep_deg(a, b):
    return math.degrees(math.atan2(math.sqrt(_dot(_cross(a, b), _cross(a, b))),
                                   _dot(a, b)))


# --------------------------------------------------------------------------- #
# The smoother
# --------------------------------------------------------------------------- #

class ReportSmoother:
    """Holds the reported pointing between polls. Not thread-safe by itself:
    the caller serializes ``update`` (comms does, under its filter lock)."""

    def __init__(self, gain=DEFAULT_GAIN, snap_deg=DEFAULT_SNAP_DEG,
                 rate_gain=DEFAULT_RATE_GAIN,
                 solve_jitter_deg=DEFAULT_SOLVE_JITTER_DEG,
                 solve_decay_s=DEFAULT_SOLVE_DECAY_S,
                 reset_gap_s=DEFAULT_RESET_GAP_S):
        self.gain = float(gain)
        self.rate_gain = float(rate_gain)
        self.snap_deg = float(snap_deg)
        self.solve_jitter_deg = float(solve_jitter_deg)
        self.solve_decay_s = float(solve_decay_s)
        self.reset_gap_s = float(reset_gap_s)
        self.reset()

    def reset(self):
        self.offset = (0.0, 0.0, 0.0)   # sky rotation applied to the target
        self.report = None              # last reported unit vector
        self.target = None              # last target unit vector
        self.omega = (0.0, 0.0, 0.0)    # target angular velocity (rad/s) on one anchor
        self.drift = (0.0, 0.0, 0.0)    # learned offset drift (rad/s), IMU source only
        self.t_anchor = None            # when the current anchor took over
        self.key = None                 # (source, anchor) of the last target
        self.t = None
        self.last_event = None          # "solve" / "switch" / "snap" (diagnostics)

    def update(self, target_radec, source, anchor, now):
        """One poll. ``target_radec`` = (ra, dec) the plain path would report;
        ``source`` = "imu" or "solve"; ``anchor`` identifies the solve the
        target hangs from (changes exactly when a new solve takes over);
        ``now`` = monotonic seconds. Returns the (ra, dec) to report."""
        tgt = radec_to_vec(*target_radec)
        if self.report is None or self.t is None or now - self.t > self.reset_gap_s:
            self.reset()
            self.report, self.target, self.key, self.t = tgt, tgt, (source, anchor), now
            self.t_anchor = now
            return target_radec
        key = (source, anchor)
        dt = max(0.0, now - self.t)
        if key == self.key and dt > 1e-6:
            # Same anchor: how fast the target is moving (the IMU's motion),
            # to carry the report forward across an anchor change.
            w = rotvec_between(self.target, tgt)
            self.omega = (w[0] / dt, w[1] / dt, w[2] / dt)
        if source == "imu" and self.key is not None and self.key[0] == "imu":
            self.offset = tuple(self.offset[i] + self.drift[i] * dt for i in range(3))
        if source == "solve" and self.solve_decay_s > 0:
            f = math.exp(-dt / self.solve_decay_s)
            self.offset = (self.offset[0] * f, self.offset[1] * f, self.offset[2] * f)
        if key != self.key:
            # Where the report would be NOW without the new anchor: the last
            # report carried forward by the motion (the gap must not count
            # the slew between polls as error). Static for the solve source.
            if source == "imu":
                # Old target carried on by the IMU motion, with the offset
                # (already advanced by the learned drift) applied.
                old_tgt = rotate(self.target, (self.omega[0] * dt, self.omega[1] * dt,
                                               self.omega[2] * dt))
                prev = rotate(old_tgt, self.offset)
            else:
                prev = self.report
                self.omega = (0.0, 0.0, 0.0)
            gap = sep_deg(prev, tgt)
            switched = self.key is None or source != self.key[0]
            if switched:
                g, ev = 0.0, "switch"
            elif source == "imu":
                g, ev = (self.gain, "solve") if gap <= self.snap_deg else (1.0, "snap")
            else:
                g, ev = ((self.gain, "solve") if gap <= self.solve_jitter_deg
                         else (1.0, "follow"))
            if gap > self.snap_deg:
                g, ev = 1.0, "snap"
            r = rotvec_between(tgt, prev)
            self.offset = (r[0] * (1 - g), r[1] * (1 - g), r[2] * (1 - g))
            if ev == "solve" and source == "imu":
                # Residual over the time this anchor ran -> drift correction.
                span = max(1e-3, now - (self.t_anchor if self.t_anchor is not None else now))
                d = tuple(self.drift[i] - self.rate_gain * r[i] / span for i in range(3))
                lim = (DRIFT_MAX_FRAC * math.sqrt(_dot(self.omega, self.omega))
                       + math.radians(DRIFT_FLOOR_DEG_S))
                n = math.sqrt(_dot(d, d))
                self.drift = d if n <= lim else tuple(c * lim / n for c in d)
            else:
                self.drift = (0.0, 0.0, 0.0)
            self.t_anchor = now
            self.last_event = ev
            self.key = key
        out = rotate(tgt, self.offset)
        self.report, self.target, self.t = out, tgt, now
        return vec_to_radec(out)

    def status(self):
        return {"offset_deg": math.degrees(math.sqrt(_dot(self.offset, self.offset))),
                "drift_deg_s": math.degrees(math.sqrt(_dot(self.drift, self.drift))),
                "source": self.key[0] if self.key else None,
                "last_event": self.last_event}


# --------------------------------------------------------------------------- #
# Mid-exposure attitude
# --------------------------------------------------------------------------- #

def _nlerp(q0, q1, f):
    d = sum(a * b for a, b in zip(q0, q1))
    if d < 0:                                   # shortest path
        q1 = tuple(-c for c in q1)
    q = tuple(a + (b - a) * f for a, b in zip(q0, q1))
    n = math.sqrt(sum(c * c for c in q)) or 1.0
    return tuple(c / n for c in q)


def interp_quat(history, t, slack_s=0.15):
    """IMU quaternion at monotonic time ``t`` from ``history`` = sequence of
    (t, q) in time order (nlerp between the bracketing samples; the samples are
    0.1 s apart, where nlerp equals slerp to well under an arcsecond).
    Outside the history by more than ``slack_s``: None. Within it: the
    nearest end sample."""
    h = [(float(ts), tuple(q)) for ts, q in (history or ()) if q is not None]
    if not h:
        return None
    if t < h[0][0] - slack_s or t > h[-1][0] + slack_s:
        return None
    if t <= h[0][0]:
        return h[0][1]
    if t >= h[-1][0]:
        return h[-1][1]
    for (t0, q0), (t1, q1) in zip(h, h[1:]):
        if t0 <= t <= t1:
            return q0 if t1 <= t0 else _nlerp(q0, q1, (t - t0) / (t1 - t0))
    return h[-1][1]


def mid_exposure_monotonic(meta, boottime_minus_monotonic_s):
    """Monotonic time of the middle of a frame's exposure, from a frame_meta
    entry dict (``sensor_timestamp_ns`` = CLOCK_BOOTTIME at readout start,
    ``exposure_us``). None if incomplete."""
    try:
        ts = int(meta["sensor_timestamp_ns"]) / 1e9
        exp = int(meta["exposure_us"]) / 1e6
    except (KeyError, TypeError, ValueError):
        return None
    return ts - exp / 2.0 - float(boottime_minus_monotonic_s)
