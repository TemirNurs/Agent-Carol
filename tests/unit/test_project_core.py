"""Tests for scripts._lib.projects — project_core, slugify, normalize_name.

These tests guard the dedupe-key invariant: same project, ANY name format,
MUST produce the same project_core. This is the rule that was broken on
2026-05-22 and caused duplicate CRM rows.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "scripts"))
from _lib.projects import project_core, slugify, normalize_name, same_project


# -----------------------------------------------------------------------------
# project_core — format-agnostic dedupe key
# -----------------------------------------------------------------------------

def test_project_core_food_lion_format_variants():
    """All Food Lion #2235 name formats must hash to the same key."""
    variants = [
        "Food Lion #2235 Quinton, VA",
        "2235 Food Lion Quinton, VA",
        "Food Lion 2235 Quinton VA",
        "Follow-Up: Food Lion #2235 Quinton, VA (BID-0024)",
        "Re: Food Lion #2235 Quinton, VA",
        "2235 Food Lion Quinton, VA:",
    ]
    cores = {project_core(v) for v in variants}
    assert len(cores) == 1, f"variants produced different keys: {cores}"
    assert cores.pop() == ("2235", "food lion")


def test_project_core_letter_suffix_2118b():
    """Store numbers like 2118B / 2671B must capture the digit prefix."""
    assert project_core("Food Lion 2118B - Dinwiddie")        == ("2118", "food lion")
    assert project_core("Food Lion #2118B Dinwiddie")          == ("2118", "food lion")
    assert project_core("2118B Food Lion Dinwiddie")           == ("2118", "food lion")


def test_project_core_distinguishes_stores():
    """Different store numbers MUST produce different keys."""
    assert project_core("Food Lion #2235 Quinton") != project_core("Food Lion #2219 Quinton")
    assert project_core("Food Lion 2118B Dinwiddie") != project_core("Food Lion 2671B Petersburg")
    assert project_core("Food Lion 1602 Chesterfield") != project_core("Food Lion 2541 Chester")


def test_project_core_handles_no_number():
    """Projects without a store number still get a usable key."""
    assert project_core("Midtown East (Buildings 1, 2 & 3)") == ("", "midtown east")
    assert project_core("TMSA Concord Grandstands") == ("", "tmsa concord")  # 'grandstands' is stopword


def test_project_core_handles_none_and_empty():
    assert project_core(None) == ("", "")
    assert project_core("") == ("", "")
    assert project_core("   ") == ("", "")


def test_project_core_strips_re_fwd_prefixes():
    assert project_core("RE: Food Lion #2235 Quinton") == ("2235", "food lion")
    assert project_core("Fwd: Food Lion #2235 Quinton") == ("2235", "food lion")
    assert project_core("FW: Food Lion #2235 Quinton") == ("2235", "food lion")


def test_project_core_strips_ccf_prefixes_and_suffixes():
    """CCF / Proposal / Painting boilerplate must not pollute the key."""
    assert project_core("CCF Proposal — Food Lion #2235 Quinton, VA — Painting") \
        == project_core("Food Lion #2235 Quinton, VA")


# -----------------------------------------------------------------------------
# slugify — position-sensitive (used for filesystem / lookup keys)
# -----------------------------------------------------------------------------

def test_slugify_basic():
    assert slugify("Food Lion #2235 Quinton, VA") == "food-lion-2235-quinton-va"
    assert slugify("Carvana / Adesa Reconditioning Facility") == "carvana-adesa-reconditioning-facility"


def test_slugify_handles_none_and_empty():
    assert slugify(None) == ""
    assert slugify("") == ""


def test_slugify_caps_at_80_chars():
    long = "Food Lion " + "x" * 200
    assert len(slugify(long)) == 80


# -----------------------------------------------------------------------------
# same_project — explicit convenience wrapper
# -----------------------------------------------------------------------------

def test_same_project_format_variants():
    assert same_project("Food Lion #2235 Quinton, VA", "2235 Food Lion Quinton, VA")
    assert same_project("Food Lion 2118B Dinwiddie", "2118B Food Lion Dinwiddie")


def test_same_project_distinguishes():
    assert not same_project("Food Lion #2235 Quinton", "Food Lion #2219 Quinton")
    assert not same_project("Food Lion 2118B Dinwiddie", "Food Lion 2671B Petersburg")


def test_same_project_empty_is_not_same():
    """Two empty strings should NOT be considered the same project."""
    assert not same_project(None, None)
    assert not same_project("", "")


# -----------------------------------------------------------------------------
# normalize_name — duplicate-half cleanup ('X - Y - X - Y' → 'X - Y')
# -----------------------------------------------------------------------------

def test_normalize_name_dedup_repeating_halves():
    assert normalize_name("Food Lion 2118B - Dinwiddie - Food Lion 2118B - Dinwiddie") \
        == "Food Lion 2118B - Dinwiddie"
    assert normalize_name("Food Lion 2671B - Petersburg - Food Lion 2671B - Petersburg") \
        == "Food Lion 2671B - Petersburg"


def test_normalize_name_keeps_legit_dash_separated_names():
    """Names with legitimate dash separators should NOT be collapsed."""
    assert normalize_name("Carvana / Adesa Reconditioning Facility") \
        == "Carvana / Adesa Reconditioning Facility"
    assert normalize_name("Food Lion #1336 Quinton, VA") == "Food Lion #1336 Quinton, VA"


def test_normalize_name_strips_re_fwd():
    assert normalize_name("RE: Food Lion #2235") == "Food Lion #2235"
    assert normalize_name("Following up: Midtown East") == "Midtown East"
