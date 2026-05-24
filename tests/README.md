# Solver benchmark scripts

Run all scripts as root using the venv Python:

```bash
sudo /opt/efinder/venv/bin/python3 tests/<script>.py
```

## Test image location

The test image path is passed as a command-line argument when efinder starts.
Find the current path with:

```bash
cat /etc/systemd/system/efinder.service | grep ExecStart
# or
ps aux | grep efinder | grep test-image
```

Typically the image lives at the path specified in the service file,
often something like `/var/lib/efinder/test.png` or
`/home/efinder/test.npy`.

## bench_tetra_hints.py

Pulls a live frame from cedar-detect via SHM, then runs
`solve_from_centroids` with a range of `hint_uncertainty_deg` values
(5.0 → 0.05) to show the relationship between hint tightness and solve
speed. Confirms why 0.1° gives ~11ms while 5.0° gives ~480ms.

Note: `db.solve()` (the old RA/Dec-hint API from `efinder_cli_tetra3rs_mp`)
does not exist in the current tetra3rs version. `solve_from_centroids`
with `attitude_hint` is the equivalent.

## bench_cedar_vs_tetra.py

Runs N iterations of both backends back-to-back and prints a
side-by-side timing table:
- Cedar: cedar-detect gRPC extraction + tetra3 Python solve
- Tetra hybrid: cedar-detect gRPC extraction + tetra3rs Rust solve
