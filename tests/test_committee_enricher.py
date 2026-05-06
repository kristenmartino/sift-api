"""Tests for services/committee_enricher.py (Phase 3.F.3).

Pure-function tests for the indexing + name-stripping logic. The DB
update path is exercised through `refresh_committees()` integration
tests in CI; here we cover the deterministic transforms.
"""
from __future__ import annotations

from services.committee_enricher import (
    _strip_prefix,
    build_bioguide_to_committees,
)


# ── _strip_prefix ─────────────────────────────────────────


def test_strip_prefix_chamber_variants():
    assert _strip_prefix("Senate Committee on Finance") == "Finance"
    assert _strip_prefix("House Committee on Appropriations") == "Appropriations"
    assert _strip_prefix("Joint Committee on Taxation") == "Taxation"


def test_strip_prefix_handles_leading_the():
    assert _strip_prefix("Senate Committee on the Judiciary") == "Judiciary"
    assert _strip_prefix("House Committee on the Budget") == "Budget"


def test_strip_prefix_joint_of_congress():
    assert (
        _strip_prefix("Joint Committee of Congress on the Library") == "Library"
    )
    assert (
        _strip_prefix("Joint Committee of Congress on Printing") == "Printing"
    )


def test_strip_prefix_keeps_select_special():
    """Select / Special qualifiers stay — they're editorial signal."""
    assert (
        _strip_prefix("Senate Select Committee on Intelligence")
        == "Senate Select Committee on Intelligence"
    )
    assert (
        _strip_prefix("Senate Special Committee on Aging")
        == "Senate Special Committee on Aging"
    )


def test_strip_prefix_no_match_passthrough():
    assert _strip_prefix("Some Other Committee") == "Some Other Committee"
    assert _strip_prefix("") == ""


# ── build_bioguide_to_committees ─────────────────────────


def _committees_fixture():
    return [
        {"thomas_id": "SSAF", "name": "Senate Committee on Agriculture, Nutrition, and Forestry"},
        {"thomas_id": "SSFI", "name": "Senate Committee on Finance"},
        {"thomas_id": "HSAG", "name": "House Committee on Agriculture"},
        {"thomas_id": "JSPR", "name": "Joint Committee on Printing"},
        {"name": "Orphan (no thomas_id)"},
        {"thomas_id": "BAD"},  # no name
    ]


def _membership_fixture():
    return {
        "SSAF": [
            {"bioguide": "B001236"},
            {"bioguide": "M000355"},
        ],
        "SSFI": [
            {"bioguide": "S000148"},
            {"bioguide": "W000817"},
        ],
        "HSAG": [
            {"bioguide": "F000466"},
        ],
        "JSPR": [
            {"bioguide": "M000355"},  # McConnell on multiple
        ],
        "BAD": "not a list",
        "ORPHAN15": [{"bioguide": "X000001"}],  # subcommittee — no parent
    }


def test_build_bioguide_to_committees_indexes_correctly():
    index = build_bioguide_to_committees(
        _committees_fixture(), _membership_fixture(),
    )
    # McConnell is on Agriculture + Printing.
    assert index["M000355"] == ["Agriculture, Nutrition, and Forestry", "Printing"]
    # Sorted alphabetically within each member's list (stable diffs on re-run).
    assert index["S000148"] == ["Finance"]
    assert index["F000466"] == ["Agriculture"]
    assert index["B001236"] == ["Agriculture, Nutrition, and Forestry"]
    assert index["W000817"] == ["Finance"]


def test_build_bioguide_to_committees_drops_subcommittees():
    index = build_bioguide_to_committees(
        _committees_fixture(), _membership_fixture(),
    )
    # ORPHAN15 isn't in committees-current → that bioguide gets nothing.
    assert "X000001" not in index


def test_build_bioguide_to_committees_skips_malformed_inputs():
    """Missing thomas_id, missing name, non-dict committees, non-list members
    all get silently skipped."""
    index = build_bioguide_to_committees(
        [
            {"thomas_id": "SSFI", "name": "Senate Committee on Finance"},
            "not a dict",
            None,
            {"thomas_id": "JUNK"},  # no name
            {"name": "Orphan"},  # no thomas_id
        ],
        {
            "SSFI": [
                {"bioguide": "G001"},
                "not a dict",
                {},  # no bioguide
                {"bioguide": ""},
                {"bioguide": None},
                {"bioguide": "G002"},
            ],
            "JUNK": [{"bioguide": "DROP_ME"}],  # JUNK lost its name; dropped
        },
    )
    assert index == {"G001": ["Finance"], "G002": ["Finance"]}


def test_build_bioguide_to_committees_dedup_within_member():
    index = build_bioguide_to_committees(
        _committees_fixture(),
        {"SSAF": [{"bioguide": "M000355"}, {"bioguide": "M000355"}]},  # dup
    )
    assert index["M000355"] == ["Agriculture, Nutrition, and Forestry"]


def test_build_bioguide_to_committees_handles_none_inputs():
    """None committees or None membership returns {} cleanly."""
    assert build_bioguide_to_committees(None, _membership_fixture()) == {}
    assert build_bioguide_to_committees(_committees_fixture(), None) == {}
    assert build_bioguide_to_committees(None, None) == {}


def test_build_bioguide_to_committees_handles_wrong_type_inputs():
    """Non-list committees / non-dict membership return {} cleanly."""
    assert build_bioguide_to_committees("not a list", {}) == {}
    assert build_bioguide_to_committees([], "not a dict") == {}
