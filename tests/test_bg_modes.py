"""bg_modes registry (v0.11.48): the single source of truth for per-mode
background-subtraction facts. Pins that the derivations (bg_cache's
cache-compatibility set and model-kind routing, comms' validation set) agree
with the historical hand-maintained values, so replacing four synced copies
with one table changed nothing.
"""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from diofinder import bg_modes
from diofinder.config import Config


def test_all_historical_modes_present_in_order():
    assert bg_modes.MODE_NAMES == (
        "row_percentile", "line_median", "column_percentile",
        "row_column_percentile", "block_percentile", "uniform_mean",
        "top_hat", "temporal_median")


def test_size_params_are_real_config_fields():
    cfg = Config()
    for name, d in bg_modes.MODES.items():
        if d["size_param"] is not None:
            assert hasattr(cfg, d["size_param"]), (name, d["size_param"])


def test_cache_kinds_valid_and_match_engine_history():
    kinds = {n: d["cache_kind"] for n, d in bg_modes.MODES.items()}
    assert all(k in ("row", "block", "image", None) for k in kinds.values())
    # The historical hand-maintained facts:
    assert kinds["row_percentile"] == "row"
    assert kinds["line_median"] == "row"
    assert kinds["top_hat"] == "row"
    assert kinds["block_percentile"] == "block"
    assert kinds["temporal_median"] == "image"
    assert kinds["column_percentile"] is None
    assert kinds["row_column_percentile"] is None
    assert kinds["uniform_mean"] is None


def test_derived_cache_compatible_modes_unchanged():
    from diofinder.bg_cache import CACHE_COMPATIBLE_MODES
    assert CACHE_COMPATIBLE_MODES == frozenset(
        {"row_percentile", "line_median", "top_hat"})


def test_only_temporal_median_lacks_a_per_frame_form():
    no_pf = [n for n, d in bg_modes.MODES.items() if not d["per_frame_form"]]
    assert no_pf == ["temporal_median"]


def test_noise_row_pairing_matches_ui_history():
    noisy = {n for n, d in bg_modes.MODES.items() if d["noise_row"]}
    assert noisy == {"uniform_mean", "block_percentile"}


def test_for_ui_shape():
    ui = bg_modes.for_ui()
    assert [m["name"] for m in ui] == list(bg_modes.MODE_NAMES)
    assert all(set(m) == {"name", "label", "size_param", "size_word",
                          "per_frame_form", "noise_row", "cache_kind"}
               for m in ui)


def test_registry_is_importable_without_native_wheels():
    # The module must stay stdlib-pure so webui/comms can import it without
    # star_detect; a native import sneaking in would break the comms process.
    import ast as _ast
    import importlib
    src = pathlib.Path(bg_modes.__file__).read_text()
    imported = set()
    for node in _ast.walk(_ast.parse(src)):
        if isinstance(node, _ast.Import):
            imported.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, _ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    assert imported <= {"collections"}, imported
    importlib.reload(bg_modes)
