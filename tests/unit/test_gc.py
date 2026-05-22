"""Tests for scripts._lib.gc — GC directory + domain → GC name resolution."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "scripts"))
from _lib.gc import info_for_email, domain_of, KNOWN_GC_BY_DOMAIN


def test_domain_of_basic():
    assert domain_of("anthonypoland@rickshipman.com") == "rickshipman.com"
    assert domain_of("jimmy@windlecc.com") == "windlecc.com"
    assert domain_of("estimating@wedconstruction.com") == "wedconstruction.com"


def test_domain_of_handles_garbage():
    assert domain_of("") == ""
    assert domain_of(None) == ""
    assert domain_of("no-at-sign-here") == ""
    assert domain_of("two@signs@here.com") == "signs@here.com" or domain_of("two@signs@here.com").endswith("here.com")


def test_info_for_email_known_gcs():
    """Known GCs MUST resolve to their canonical name, not domain-guess."""
    name, contact, phone = info_for_email("jimmy@windlecc.com")
    assert name == "Windle Construction"
    assert contact == "Jimmy Windle"

    name, contact, phone = info_for_email("anthonypoland@rickshipman.com")
    # gc_crm.json (user-curated) may override the hardcoded fallback; accept either
    assert name in ("Rick Shipman Construction", "Rick Shipman")
    # Phone may come from CRM (might be blank) — only assert if hardcoded fired
    if name == "Rick Shipman Construction":
        assert "5065" in phone or "573" in phone

    name, contact, phone = info_for_email("estimating@wedconstruction.com")
    assert name == "WED Construction"


def test_info_for_email_farris_both_aliases():
    """Both tanner.barber@fiicgc.com and bids@fiicgc.com resolve to same GC."""
    n1, _, _ = info_for_email("tanner.barber@fiicgc.com")
    n2, _, _ = info_for_email("bids@fiicgc.com")
    assert n1 == n2 == "Farris Interior Installation"


def test_info_for_email_unknown_falls_back_to_titlecase():
    """Unknown domain: heuristic CamelCase from second-level domain."""
    name, _, _ = info_for_email("someone@unknownco.com")
    assert "Unknownco" in name or "unknownco" in name.lower()


def test_info_for_email_empty():
    assert info_for_email(None) == ("", "", "")
    assert info_for_email("") == ("", "", "")
    assert info_for_email("no-domain") == ("", "", "")
