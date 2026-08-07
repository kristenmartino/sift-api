"""Tests for scripts/strip_common_noun_outlet_links.py.

The script deletes stored links, so the routing decision is the whole risk:
dropping one chip too many is unrecoverable except from the backup file. Each
case below is a real prod surface form from the 2026-08-06 audit.
"""
from __future__ import annotations

import importlib.util
import os

import pytest

from services.entity_linker import _REGEX_INELIGIBLE_NAMES

_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "scripts", "strip_common_noun_outlet_links.py",
)
_spec = importlib.util.spec_from_file_location("strip_common_noun_outlet_links", _PATH)
strip = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(strip)


def _link(surface: str, cid: str = "stat-news", type_: str = "outlet") -> dict:
    return {"type": type_, "canonical_id": cid, "surface_form": surface}


@pytest.mark.parametrize("surface", ["stat", "Stat", " stat ", "STat"])
def test_bare_word_in_a_non_outlet_casing_is_dropped(surface):
    """The 111 measured false positives: 'stat' x61 and 'Stat' x50."""
    assert strip.is_common_noun_link(_link(surface)) is True


@pytest.mark.parametrize("surface", ["STAT", "STAT News", "STAT news"])
def test_the_outlets_own_styling_is_kept(surface):
    """751 'STAT' + 11 'STAT News' chips, every one on STAT News's own copy.

    They are the self-reference problem, which has its own fix — this script
    must leave them for it rather than quietly doing half of someone else's
    job.
    """
    assert strip.is_common_noun_link(_link(surface)) is False


def test_other_entities_are_never_touched():
    """Only canonical_ids in COMMON_NOUN_OUTLETS are in scope. 'the athletic'
    and 'variety' are measurably false too but are NOT blocked names, so
    deleting them here would delete links the linker recreates next pass."""
    assert strip.is_common_noun_link(_link("the athletic", "the-athletic")) is False
    assert strip.is_common_noun_link(_link("variety", "variety")) is False
    assert strip.is_common_noun_link(_link("stat", "some-org", "org")) is False
    assert strip.is_common_noun_link({"type": "outlet"}) is False
    assert strip.is_common_noun_link("not a dict") is False


def test_rewrite_preserves_every_other_link_in_order():
    links = [
        {"type": "politician", "canonical_id": "S000148", "surface_form": "Chuck Schumer"},
        _link("Stat"),
        {"type": "outlet", "canonical_id": "espn", "surface_form": "ESPN"},
    ]
    out, counts = strip.rewrite(links)
    assert out == [links[0], links[2]]
    assert counts["dropped:stat-news"] == 1


def test_rewrite_is_idempotent():
    links = [_link("stat"), _link("STAT")]
    once, _ = strip.rewrite(links)
    twice, counts = strip.rewrite(once)
    assert once == twice == [_link("STAT")]
    assert not counts


def test_every_rule_targets_a_name_the_linker_actually_blocks():
    """The script's own refusal guard, asserted rather than trusted: a rule for
    an unblocked name would delete links that the next regex pass recreates."""
    for _cid, (blocked, _styled) in strip.COMMON_NOUN_OUTLETS.items():
        assert blocked in _REGEX_INELIGIBLE_NAMES
