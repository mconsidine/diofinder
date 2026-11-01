"""mp_util.snapshot must take ONE Manager round-trip, unlike dict(proxy)."""
import multiprocessing as mp

from diofinder.mp_util import snapshot


def test_snapshot_plain_dict_is_copy():
    d = {"a": 1}
    s = snapshot(d)
    assert s == d and s is not d and type(s) is dict


def test_snapshot_mapping_without_copy():
    class M:
        def keys(self):
            return ["x"]

        def __getitem__(self, k):
            return 7
    assert snapshot(M()) == {"x": 7}


def test_snapshot_manager_proxy_single_rpc():
    with mp.Manager() as m:
        d = m.dict({f"k{i}": i for i in range(40)})
        calls = []
        orig = d._callmethod

        def counting(name, *a, **k):
            calls.append(name)
            return orig(name, *a, **k)

        d._callmethod = counting
        s = snapshot(d)
        assert type(s) is dict and s == {f"k{i}": i for i in range(40)}
        assert calls == ["copy"]
        calls.clear()
        dict(d)                       # the old way: keys() + one RPC per key
        assert len(calls) == 41
