"""Tests for scripts._lib.gc — GC directory + domain → GC name resolution.

The real GC directory (gc_directory.json / gc_crm.json) is gitignored and holds
third-party contact PII, so CI must NOT depend on it. These tests monkeypatch
KNOWN_GC_BY_DOMAIN with a FICTIONAL fixture and assert resolution against those
fictional values, exercising the same functions (domain_of, info_for_email,
alias resolution) without shipping any real contact data.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "scripts"))
from _lib import gc as gc_mod
from _lib.gc import info_for_email, domain_of


# Fictional directory fixture — no real GC/contact data.
FAKE_DIRECTORY = {
    "examplegc.com": ("Example GC", "Jane Doe", "555-000-1111"),
    "buildco.test": ("BuildCo Test", "John Smith", "555-000-2222"),
}


@pytest.fixture(autouse=True)
def _patch_directory(monkeypatch):
    """Swap in the fictional directory and disable the gc_crm.json cache so
    resolution is deterministic and never reads the gitignored real data."""
    monkeypatch.setattr(gc_mod, "KNOWN_GC_BY_DOMAIN", FAKE_DIRECTORY)
    monkeypatch.setattr(gc_mod, "_GC_BY_EMAIL_CACHE", {})


def test_domain_of_basic():
    assert domain_of("jane@examplegc.com") == "examplegc.com"
    assert domain_of("john@buildco.test") == "buildco.test"
    assert domain_of("estimating@examplegc.com") == "examplegc.com"


def test_domain_of_handles_garbage():
    assert domain_of("") == ""
    assert domain_of(None) == ""
    assert domain_of("no-at-sign-here") == ""
    assert domain_of("two@signs@here.com") == "signs@here.com" or domain_of("two@signs@here.com").endswith("here.com")


def test_info_for_email_known_gcs():
    """Known (fictional) GCs MUST resolve to their canonical name + contact +
    phone, not a domain-guess."""
    name, contact, phone = info_for_email("jane@examplegc.com")
    assert name == "Example GC"
    assert contact == "Jane Doe"
    assert phone == "555-000-1111"

    name, contact, phone = info_for_email("john@buildco.test")
    assert name == "BuildCo Test"
    assert contact == "John Smith"


def test_info_for_email_both_aliases():
    """Two different inboxes at the same domain resolve to the same GC."""
    n1, _, _ = info_for_email("jane.doe@examplegc.com")
    n2, _, _ = info_for_email("bids@examplegc.com")
    assert n1 == n2 == "Example GC"


def test_info_for_email_unknown_falls_back_to_titlecase():
    """Unknown domain: heuristic CamelCase from second-level domain."""
    name, _, _ = info_for_email("someone@unknownco.com")
    assert "Unknownco" in name or "unknownco" in name.lower()


def test_info_for_email_empty():
    assert info_for_email(None) == ("", "", "")
    assert info_for_email("") == ("", "", "")
    assert info_for_email("no-domain") == ("", "", "")
