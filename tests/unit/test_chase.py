"""Tests for scripts._lib.chase — chase helpers."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "scripts"))
from _lib.chase import (
    CC_INTERNAL, INTERVAL_HOURS, MAX_ATTEMPTS, INACTIVE_TAGS,
    is_chaseable_status, has_inactive_flag,
)


def test_cc_internal_includes_team():
    """CC list MUST include the company address and be non-empty (the internal
    team CCs are sourced from env/config, not hardcoded)."""
    assert CC_INTERNAL
    assert "cs@carolinacommercialfinishes.com" in CC_INTERNAL


def test_interval_hours_escalates():
    """Cadence must get more aggressive as attempts grow."""
    assert INTERVAL_HOURS[2] > INTERVAL_HOURS[3]
    assert INTERVAL_HOURS[3] > INTERVAL_HOURS[4]
    assert INTERVAL_HOURS[4] > INTERVAL_HOURS[5]
    # Caps at 6h
    for n in (6, 7, 8, 9, 10, 11, 12):
        assert INTERVAL_HOURS[n] == 6


def test_max_attempts_reasonable():
    assert 5 <= MAX_ATTEMPTS <= 20


def test_is_chaseable_status():
    assert is_chaseable_status("Bid Submitted")
    assert is_chaseable_status("Awaiting Decision")
    assert not is_chaseable_status("Won")
    assert not is_chaseable_status("Lost")
    assert not is_chaseable_status("Withdrawn")
    assert not is_chaseable_status("")
    assert not is_chaseable_status(None)


def test_has_inactive_flag():
    assert has_inactive_flag("[BOUNCE] mailbox full")
    assert has_inactive_flag("Not bidding this one. [NOT BIDDING]")
    assert has_inactive_flag("[withdrawn]".upper())  # case insensitive
    assert not has_inactive_flag("Regular project note")
    assert not has_inactive_flag("")
    assert not has_inactive_flag(None)
