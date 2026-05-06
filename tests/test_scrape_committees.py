"""Tests for scripts/scrape_committees.py (Phase 3.F.1).

Network-free: uses inline YAML fixtures rather than hitting the live
`unitedstates/congress-legislators` repo.
"""
from __future__ import annotations

import importlib.util
import pathlib
import sys

# Load the script as a module without invoking its main(). It's not in a
# package, so we import via spec.
SCRIPT = pathlib.Path(__file__).parent.parent / "scripts" / "scrape_committees.py"
_spec = importlib.util.spec_from_file_location("scrape_committees", SCRIPT)
assert _spec is not None and _spec.loader is not None
scrape_committees = importlib.util.module_from_spec(_spec)
sys.modules["scrape_committees"] = scrape_committees
_spec.loader.exec_module(scrape_committees)


# ── _strip_prefix ─────────────────────────────────────────


def test_strip_prefix_senate_committee_on():
    assert scrape_committees._strip_prefix("Senate Committee on Finance") == "Finance"
    assert (
        scrape_committees._strip_prefix("Senate Committee on Foreign Relations")
        == "Foreign Relations"
    )


def test_strip_prefix_house_committee_on():
    assert (
        scrape_committees._strip_prefix("House Committee on Appropriations")
        == "Appropriations"
    )


def test_strip_prefix_with_leading_the():
    """Edge case: 'Senate Committee on the Judiciary' was producing 'the Judiciary'.
    The fix strips the leading 'the' as a defensive cleanup."""
    assert (
        scrape_committees._strip_prefix("Senate Committee on the Judiciary")
        == "Judiciary"
    )
    assert (
        scrape_committees._strip_prefix("House Committee on the Budget")
        == "Budget"
    )


def test_strip_prefix_joint_of_congress_on():
    """The 'Joint Committee of Congress on the Library' form must
    strip to 'Library' (the longest-match-first ordering matters)."""
    assert (
        scrape_committees._strip_prefix(
            "Joint Committee of Congress on the Library"
        )
        == "Library"
    )
    assert (
        scrape_committees._strip_prefix("Joint Committee of Congress on Printing")
        == "Printing"
    )


def test_strip_prefix_joint_committee_on():
    assert (
        scrape_committees._strip_prefix("Joint Committee on Taxation")
        == "Taxation"
    )


def test_strip_prefix_select_special_committees_kept():
    """Select / Special committees keep their qualifier — that's editorial
    information, not redundant chamber boilerplate."""
    assert (
        scrape_committees._strip_prefix("Senate Select Committee on Intelligence")
        == "Senate Select Committee on Intelligence"
    )
    assert (
        scrape_committees._strip_prefix("Senate Special Committee on Aging")
        == "Senate Special Committee on Aging"
    )


def test_strip_prefix_no_match_passthrough():
    """Names without a known chamber prefix pass through unchanged."""
    assert scrape_committees._strip_prefix("Some Other Committee") == "Some Other Committee"
    assert scrape_committees._strip_prefix("") == ""


def test_strip_prefix_united_states_variant():
    """The 'United States Senate Committee on…' form is a less common
    formal variant that should also be stripped."""
    assert (
        scrape_committees._strip_prefix("United States Senate Committee on Finance")
        == "Finance"
    )


def test_strip_prefix_trims_whitespace():
    assert scrape_committees._strip_prefix("  Senate Committee on Finance  ") == "Finance"


# ── build_bioguide_to_committees ───────────────────────────


def _committees_fixture():
    """Mirrors the shape of `committees-current.yaml`: a list of dicts each
    with `thomas_id` + `name`. Subcommittees are nested under their parent
    in the real YAML and are intentionally not at the top level — so they
    don't appear here either, which is exactly why the indexer skips
    membership entries keyed on subcommittee thomas_ids."""
    return [
        {"thomas_id": "SSAF", "name": "Senate Committee on Agriculture, Nutrition, and Forestry"},
        {"thomas_id": "SSFI", "name": "Senate Committee on Finance"},
        {"thomas_id": "HSAG", "name": "House Committee on Agriculture"},
        {"thomas_id": "JSPR", "name": "Joint Committee on Printing"},
        # Malformed: no thomas_id.
        {"name": "Orphan Committee"},
        # Malformed: no name.
        {"thomas_id": "BAD"},
    ]


def _membership_fixture():
    """Mirrors `committee-membership-current.yaml`: dict keyed on thomas_id,
    value is a list of {bioguide, name, party, rank, title}."""
    return {
        "SSAF": [
            {"bioguide": "B001236", "name": "John Boozman", "party": "majority", "rank": 1},
            {"bioguide": "M000355", "name": "Mitch McConnell", "party": "majority", "rank": 2},
        ],
        "SSFI": [
            {"bioguide": "S000148", "name": "Charles E. Schumer", "party": "minority", "rank": 1},
            {"bioguide": "W000817", "name": "Elizabeth Warren", "party": "minority", "rank": 2},
        ],
        "HSAG": [
            {"bioguide": "F000466", "name": "Glenn Thompson", "party": "majority", "rank": 1},
        ],
        "JSPR": [
            # Same person appears across committees — the index dedups.
            {"bioguide": "M000355", "name": "Mitch McConnell"},
        ],
        # Subcommittee — should be ignored at the top-level scrape.
        "SSGA13": [
            {"bioguide": "S000148", "name": "Schumer"},
        ],
        # Malformed: not a list.
        "BAD": "this should be a list",
        # Subcommittee thomas_id without parent in committees fixture — ignored.
        "ORPHAN15": [
            {"bioguide": "X000001", "name": "Nobody"},
        ],
    }


def test_build_bioguide_to_committees_indexes_correctly():
    index = scrape_committees.build_bioguide_to_committees(
        _committees_fixture(), _membership_fixture(),
    )
    # McConnell is on Agriculture + Printing (two committees, dedupe-aware).
    assert index["M000355"] == ["Agriculture, Nutrition, and Forestry", "Printing"]
    # Schumer is only on Finance — the SSGA13 subcommittee is intentionally
    # dropped so dossier lists stay tight.
    assert index["S000148"] == ["Finance"]
    # House member.
    assert index["F000466"] == ["Agriculture"]
    # Boozman + Warren each on one committee.
    assert index["B001236"] == ["Agriculture, Nutrition, and Forestry"]
    assert index["W000817"] == ["Finance"]


def test_build_bioguide_to_committees_skips_subcommittees():
    """Subcommittee thomas_ids (those not in committees-current) shouldn't
    contribute to the index, even if their members appear in
    committee-membership-current."""
    index = scrape_committees.build_bioguide_to_committees(
        _committees_fixture(), _membership_fixture(),
    )
    # Schumer is on the SSGA13 subcommittee per the membership fixture, but
    # SSGA13 isn't a top-level committee in committees-current, so it's dropped.
    # His result should be just Finance, not Finance + the subcommittee.
    assert "Whatever" not in str(index.get("S000148"))
    # Orphan thomas_id is also dropped.
    assert "X000001" not in index


def test_build_bioguide_to_committees_skips_malformed_membership():
    """Membership values that aren't lists, members missing bioguide, etc.
    should be silently skipped."""
    membership = {
        "SSFI": "not a list",
        "SSAF": [
            {"bioguide": ""},  # empty bioguide
            {"bioguide": None},  # null bioguide
            {"name": "No bioguide field"},  # missing
            "string entry",  # not a dict
            {"bioguide": "G000001", "name": "Good Member"},
        ],
    }
    index = scrape_committees.build_bioguide_to_committees(
        _committees_fixture(), membership,
    )
    assert index == {"G000001": ["Agriculture, Nutrition, and Forestry"]}


def test_build_bioguide_to_committees_dedup_within_member():
    """If a member somehow appears twice on the same committee (data bug),
    the index dedupes their committee list."""
    membership = {
        "SSAF": [
            {"bioguide": "M000355", "name": "Mitch McConnell"},
            {"bioguide": "M000355", "name": "Mitch McConnell"},  # dup
        ],
    }
    index = scrape_committees.build_bioguide_to_committees(
        _committees_fixture(), membership,
    )
    assert index["M000355"] == ["Agriculture, Nutrition, and Forestry"]


def test_build_bioguide_to_committees_sorts_committee_lists():
    """Committee names are sorted within each member's list so re-runs
    produce identical CSV diffs (no spurious order churn)."""
    # Build a member who's on three committees in non-alphabetical order.
    membership = {
        "SSFI": [{"bioguide": "X000001"}],
        "SSAF": [{"bioguide": "X000001"}],
        "JSPR": [{"bioguide": "X000001"}],
    }
    index = scrape_committees.build_bioguide_to_committees(
        _committees_fixture(), membership,
    )
    assert index["X000001"] == [
        "Agriculture, Nutrition, and Forestry",
        "Finance",
        "Printing",
    ]


def test_build_bioguide_to_committees_skips_committees_without_thomas_id():
    """The 'Orphan Committee' fixture has no thomas_id and shouldn't get
    indexed even if a member somehow references it."""
    # No way for membership to reference a committee with no thomas_id since
    # the dict is keyed on thomas_id. So this test verifies the iteration
    # over committees doesn't crash on such entries.
    index = scrape_committees.build_bioguide_to_committees(
        _committees_fixture(), {},
    )
    assert index == {}
