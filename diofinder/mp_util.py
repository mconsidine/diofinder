"""Helpers for multiprocessing.Manager proxies.

``dict(proxy)`` on a ``Manager().dict()`` is NOT one IPC round-trip: CPython's
dict constructor sees a non-dict mapping, calls ``proxy.keys()`` (one RPC) and
then ``proxy[k]`` for EVERY key (one RPC each) — N+1 round-trips to the Manager
server on CPU 0. ``proxy.copy()`` is a single exposed method call that returns
a plain dict. Measured on an 81-key dict: dict(proxy) 9.3 ms vs copy() 0.14 ms
(x86; several times worse on the Zero 2W's A53).
"""


def snapshot(mapping) -> dict:
    """Plain-dict snapshot of a Manager DictProxy (or any mapping) in ONE RPC.

    Uses ``.copy()`` when available (DictProxy and dict both expose it and
    return a plain ``dict``); falls back to ``dict(mapping)`` otherwise.
    """
    copy = getattr(mapping, "copy", None)
    if copy is not None:
        out = copy()
        if isinstance(out, dict):
            return out
    return dict(mapping)
