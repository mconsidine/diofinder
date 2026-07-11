"""The background-mode registry: one declarative table, everything derives.

Before v0.11.48 the properties of each background mode lived in FOUR
hand-synchronized places: bg_cache's cache-compatibility/model-kind logic,
comms' solver_params_set validation tuple, and two separately hand-written
web-page <select> lists — which is how `temporal_median` came to be
selectable on the Advanced page but absent from the Background page. This
module is the single source of truth; the others are now derivations.

Deliberately pure (stdlib only — no star_detect import) so the webui and
comms can import it without pulling native wheels. Wheel *capability* is a
runtime fact and stays out of this table: the solver reports it via
bg_cache_status (`tophat_supported` / `block_cache_supported` /
`bg_image_supported`), and consumers combine the two.

Fields:
  label          menu text (name + short qualifier)
  cache_kind     which temporal-cache model can serve it: "row" | "block" |
                 "image" | None (None = full-frame spatial preprocessing,
                 never cached)
  size_param     the config key for its size knob, or None (this is what
                 makes exactly one size field appear in the UI per mode)
  size_word      one-word size label for compact UIs ("radius"/"tile"/...)
  capability_flag  bg_cache_status flag gating its cached path (or, for
                 top_hat, its native availability), or None
  per_frame_form True if the mode has a per-frame form the webui preview can
                 approximate (temporal_median does not: it IS the cache)
  noise_row      True if the mode pairs meaningfully with a noise-mode choice
                 in the UI (the uniform_mean+global_rms reference pairing)
"""
from collections import OrderedDict

MODES = OrderedDict([
    ("row_percentile", dict(
        label="row_percentile (default, fastest)",
        cache_kind="row", size_param=None, size_word=None,
        capability_flag=None, per_frame_form=True, noise_row=False)),
    ("line_median", dict(
        label="line_median (robust row correction)",
        cache_kind="row", size_param=None, size_word=None,
        capability_flag=None, per_frame_form=True, noise_row=False)),
    ("column_percentile", dict(
        label="column_percentile (vertical gradient removal)",
        cache_kind=None, size_param=None, size_word=None,
        capability_flag=None, per_frame_form=True, noise_row=False)),
    ("row_column_percentile", dict(
        label="row_column_percentile (separable 2-D)",
        cache_kind=None, size_param=None, size_word=None,
        capability_flag=None, per_frame_form=True, noise_row=False)),
    ("block_percentile", dict(
        label="block_percentile (bilinear tile median, fast 2-D)",
        cache_kind="block", size_param="detect_bg_block_size",
        size_word="tile", capability_flag="block_cache_supported",
        per_frame_form=True, noise_row=True)),
    ("uniform_mean", dict(
        label="uniform_mean (sliding-window mean, tetra3-compatible)",
        cache_kind=None, size_param="detect_uniform_filter_size",
        size_word="window", capability_flag=None,
        per_frame_form=True, noise_row=True)),
    ("top_hat", dict(
        label="top_hat (morphological, slowest)",
        cache_kind="row", size_param="detect_tophat_radius",
        size_word="radius", capability_flag="tophat_supported",
        per_frame_form=True, noise_row=False)),
    ("temporal_median", dict(
        label="temporal_median (2-D stack, cache-only; sycamore≥0.13)",
        cache_kind="image", size_param=None, size_word=None,
        capability_flag="bg_image_supported", per_frame_form=False,
        noise_row=False)),
])

MODE_NAMES = tuple(MODES)


def for_ui():
    """Menu-ready list: [{name, label, size_param, size_word,
    per_frame_form, noise_row, cache_kind}, ...] in display order."""
    return [dict(name=n, **{k: d[k] for k in (
        "label", "size_param", "size_word", "per_frame_form",
        "noise_row", "cache_kind")}) for n, d in MODES.items()]
