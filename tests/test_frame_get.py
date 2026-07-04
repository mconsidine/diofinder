"""SOLVER_OP_FRAME_GET: FrameSlots-bracketed frame served to the webui.

The webui's old direct-SHM reads could return torn frames (camera writing the
slot concurrently) and non-consecutive bursts. frame_get routes through the
solver's read path; these tests pin the op's contract with a stubbed reader.
"""
import unittest

import numpy as np

from diofinder.solver_proc import _handle_solver_cmd, _SolverState
from diofinder.worker_cmds import SolverCmd, SOLVER_OP_FRAME_GET


class _Cfg:
    frame_height = 4
    frame_width = 6
    tracking_enabled = False


def _mk_state(frames):
    """State whose read_frame serves `frames` in order, honoring after_seq."""
    state = _SolverState(solver_t3=None, cfg=_Cfg())
    seqs = list(range(100, 100 + len(frames)))

    def read_frame(with_seq=False, after_seq=-1):
        for fr, seq in zip(frames, seqs):
            if seq > after_seq:
                return (fr, seq) if with_seq else fr
        # Timeout behavior: FrameSlots returns the latest frame regardless.
        fr, seq = frames[-1], seqs[-1]
        return (fr, seq) if with_seq else fr

    state.read_frame = read_frame
    return state


class FrameGetTests(unittest.TestCase):
    def test_returns_bytes_shape_seq(self):
        fr = np.arange(24, dtype=np.uint8).reshape(4, 6)
        state = _mk_state([fr])
        reply = _handle_solver_cmd(
            SolverCmd(request_id="t", op=SOLVER_OP_FRAME_GET, args={}), None, None,
            cfg=_Cfg(), state=state)
        self.assertTrue(reply.ok, reply.error)
        self.assertEqual(reply.result["shape"], [4, 6])
        self.assertEqual(reply.result["seq"], 100)
        got = np.frombuffer(reply.result["data"], dtype=np.uint8).reshape(4, 6)
        np.testing.assert_array_equal(got, fr)

    def test_after_seq_serves_strictly_newer(self):
        frames = [np.full((4, 6), i, dtype=np.uint8) for i in range(3)]
        state = _mk_state(frames)
        reply = _handle_solver_cmd(
            SolverCmd(request_id="t", op=SOLVER_OP_FRAME_GET,
                      args={"after_seq": 100}),
            None, None, cfg=_Cfg(), state=state)
        self.assertTrue(reply.ok)
        self.assertEqual(reply.result["seq"], 101)
        self.assertEqual(int(reply.result["data"][0]), 1)

    def test_unavailable_source_errors(self):
        state = _SolverState(solver_t3=None, cfg=_Cfg())
        state.read_frame = None
        reply = _handle_solver_cmd(
            SolverCmd(request_id="t", op=SOLVER_OP_FRAME_GET, args={}), None, None,
            cfg=_Cfg(), state=state)
        self.assertFalse(reply.ok)


if __name__ == "__main__":
    unittest.main()
