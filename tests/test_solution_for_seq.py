"""Debug bundles pair each burst frame with ITS OWN solve result.

At fast frame rates (0.3 s exposures) the shared latest_solution has always
moved on by the time the web UI asks, so the old status poll attached
`solution: null` to every bundle frame (seen in a 2026-11-01 field bundle).
The solver now keeps its last ~48 seq-tagged publishes (fed by SolutionTap)
and answers SOLVER_OP_SOLUTION_FOR_SEQ from them; comms exposes it as the
`solution_for_seq` maint command."""
import types

from diofinder import comms_proc as cp
from diofinder.live_capture import SolutionTap
from diofinder.solver_proc import _handle_solver_cmd, _SolverState
from diofinder.worker_cmds import (SolverCmd, SolverCmdReply,
                                   SOLVER_OP_SOLUTION_FOR_SEQ)


class _Cfg:
    frame_height = 4
    frame_width = 6
    tracking_enabled = False


class _Rec:
    active = False


def _state_with_publishes(n=60):
    state = _SolverState(solver_t3=None, cfg=_Cfg())
    store = {}
    tap = SolutionTap(store, _Rec(), history=state.recent_solutions)
    for seq in range(1, n + 1):
        tap.update({"seq": seq, "solved": seq % 2 == 0, "matches": seq})
    tap.update({"solved": False})               # untagged publishes are ignored
    return state, store


def test_tap_keeps_a_bounded_history_of_tagged_publishes():
    state, store = _state_with_publishes()
    assert store["seq"] == 60                    # proxy still sees every update
    assert len(state.recent_solutions) == 48
    assert state.recent_solutions[0]["seq"] == 13


def test_solver_returns_each_frames_own_result():
    state, _ = _state_with_publishes()
    reply = _handle_solver_cmd(
        SolverCmd(request_id="t", op=SOLVER_OP_SOLUTION_FOR_SEQ,
                  args={"seqs": [58, 59, "60", 5, "junk"]}),
        None, None, cfg=_Cfg(), state=state)
    assert reply.ok, reply.error
    sols = reply.result["solutions"]
    assert set(sols) == {"58", "59", "60"}       # 5 has aged out; junk ignored
    assert sols["59"]["matches"] == 59 and sols["59"]["solved"] is False
    assert sols["60"]["solved"] is True


def test_comms_command_routes_to_the_solver(monkeypatch):
    calls = []

    def fake_call(op, args, q, rq, **kw):
        calls.append((op, args))
        return SolverCmdReply(request_id="x", ok=True,
                              result={"solutions": {"7": {"seq": 7}}})
    monkeypatch.setattr(cp, "_call_solver", fake_call)
    ctx = types.SimpleNamespace(solver_cmd_q=None, solver_cmd_reply_q=None,
                                cfg=types.SimpleNamespace(), shared_cfg={})
    r = cp._handle_maint_command(cp.MaintRequest(cmd="solution_for_seq",
                                                 args={"seq": 7}), ctx)
    assert r.ok and r.result["solutions"]["7"]["seq"] == 7
    assert calls == [(SOLVER_OP_SOLUTION_FOR_SEQ, {"seqs": [7]})]


def test_matched_rows_join_their_own_frame():
    from diofinder.live_capture import matched_rows
    state, _ = _state_with_publishes()
    rows = matched_rows([(10.5, 20.5)], [(100.0, 20.0, 3.1)])
    assert rows == [[20.5, 10.5, 100.0, 20.0, 3.1]]
    assert matched_rows(None, None) == []
    state.recent_matches.append((59, rows))
    reply = _handle_solver_cmd(
        SolverCmd(request_id="t", op=SOLVER_OP_SOLUTION_FOR_SEQ, args={"seqs": [59, 60]}),
        None, None, cfg=_Cfg(), state=state)
    sols = reply.result["solutions"]
    assert sols["59"]["matched"] == rows and "matched" not in sols["60"]


def test_matches_want_sets_a_lapsing_deadline(monkeypatch):
    monkeypatch.setattr(cp.time, "monotonic", lambda: 500.0)
    ctx = types.SimpleNamespace(solver_cmd_q=None, solver_cmd_reply_q=None,
                                cfg=types.SimpleNamespace(), shared_cfg={})
    r = cp._handle_maint_command(cp.MaintRequest(cmd="matches_want",
                                                 args={"seconds": 1000}), ctx)
    assert r.ok and r.result["seconds"] == 120.0       # capped
    assert ctx.shared_cfg["matches_wanted_until"] == 620.0
