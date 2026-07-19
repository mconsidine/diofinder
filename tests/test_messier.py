"""Unit tests for the centered-object (Messier DSO) label selection (messier.py).

Covers the per-object extent-based match radius, the nearest-center rule when
several objects overlap, the brightness tie-break, and the no-match (blank
pointing) case.
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from diofinder.messier import MessierCatalog


def _catalog():
    # Three objects near the pointing (ra=100, dec=20):
    #   M31  at dec 20.0, 180' (3 deg) -> match radius ~1.6 deg  (big galaxy)
    #   M57  at dec 20.5,  1.4' (tiny) -> match radius 0.25 deg  (planetary)
    #   M13  at dec 30.0,  20'         -> match radius ~0.27 deg (far away)
    return MessierCatalog(
        m=["M31", "M57", "M13"],
        name=["Andromeda Galaxy", "Ring Nebula", "Hercules Cluster"],
        ra_deg=[100.0, 100.0, 100.0],
        dec_deg=[20.0, 20.5, 30.0],
        size_arcmin=[180.0, 1.4, 20.0],
        mag=[3.4, 8.8, 5.8],
        mtype=["G", "PN", "GCl"],
    )


def test_big_object_matches_within_its_extent():
    # Aim 0.4 deg BELOW M31's center (dec 19.6): inside M31's ~1.6 deg radius,
    # but 0.9 deg from M57 -> outside its 0.25 deg circle -> only M31 matches.
    dso = _catalog().centered(100.0, 19.6)
    assert dso["m"] == "M31"
    assert dso["name"] == "Andromeda Galaxy"
    assert dso["type"] == "G"
    assert abs(dso["sep_deg"] - 0.4) < 0.05


def test_overlap_picks_nearest_center():
    # Aim exactly on M57 (dec 20.5): inside both M57 (sep 0) and M31 (sep 0.5,
    # within 1.6). Nearest center wins -> the tiny, fainter M57, not M31.
    dso = _catalog().centered(100.0, 20.5)
    assert dso["m"] == "M57"
    assert dso["sep_deg"] < 0.05


def test_no_match_returns_none():
    # Aim in blank sky: 5 deg from M31 (> 1.6), far from the tiny objects.
    assert _catalog().centered(100.0, 25.0) is None


def test_brightness_breaks_ties():
    # Two equal-extent objects symmetric about the aim point (both 1 deg away,
    # identical separation) -> the brighter (smaller mag) wins the tie.
    cat = MessierCatalog(
        m=["Mdim", "Mbright"],
        name=["dim", "bright"],
        ra_deg=[100.0, 100.0],
        dec_deg=[21.0, 19.0],
        size_arcmin=[180.0, 180.0],
        mag=[9.0, 2.0],
        mtype=["G", "G"],
    )
    dso = cat.centered(100.0, 20.0)
    assert dso["m"] == "Mbright"
    assert dso["mag"] == 2.0


def test_missing_magnitude_is_none():
    # A blank mag cell parses to NaN and is reported as None, not a number.
    cat = MessierCatalog(
        m=["M40"],
        name=["Winnecke 4"],
        ra_deg=[100.0],
        dec_deg=[20.0],
        size_arcmin=[0.8],
        mag=[float("nan")],
        mtype=["**"],
    )
    dso = cat.centered(100.0, 20.0)
    assert dso["m"] == "M40"
    assert dso["mag"] is None
