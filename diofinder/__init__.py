"""diofinder: plate-solving electronic finder for the Pi Zero 2W.

See diofinder_main.py for the launcher; the worker processes live in
camera_proc.py, solver_proc.py, and comms_proc.py. Coordination via
frame_slots.py (triple-buffered shared memory).
"""
