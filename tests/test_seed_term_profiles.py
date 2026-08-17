"""Tests for scripts/seed_term_profiles.py — the citation gate.

DB-free: `validate` is pure, so the input is a list of CSV-row dicts shaped
exactly like `csv.DictReader` yields.

What this guards: a `/term/<slug>` page states what a legal term means. The
whole reason the table exists rather than reading the ~11,900 primer
definitions already in the corpus is that those carry no source. If a row can
reach the DB without one, the table has no purpose — so the source check is
the test that matters here, and the rest of the cases exist to make sure a
malformed row is *dropped* rather than taking the run down with it.
"""
from __future__ import annotations

import importlib.util
import pathlib
import sys

SCRIPT = pathlib.Path(__file__).parent.parent / "scripts" / "seed_term_profiles.py"
_spec = importlib.util.spec_from_file_location("seed_term_profiles", SCRIPT)
assert _spec is not None and _spec.loader is not None
seed_term_profiles = importlib.util.module_from_spec(_spec)
sys.modules["seed_term_profiles"] = seed_term_profiles
_spec.loader.exec_module(seed_term_profiles)

validate = seed_term_profiles.validate

CSV_PATH = pathlib.Path(__file__).parent.parent / "data" / "term_profiles.csv"


def row(**over) -> dict:
    base = {
        "slug": "habeas-corpus",
        "term": "Habeas Corpus",
        "definition": "A court order requiring officials to justify a detention.",
        "definition_source": "https://www.law.cornell.edu/wex/habeas_corpus",
        "definition_checked": "2026-08-10",
        "aliases": '["writ of habeas corpus"]',
        "category": "courts",
        "notes": "",
    }
    base.update(over)
    return base


def slugs(accepted: list[dict]) -> list[str]:
    return [a["slug"] for a in accepted]


# --- the gate itself -------------------------------------------------------

def test_accepts_a_fully_sourced_row():
    accepted, rejected = validate([row()])
    assert slugs(accepted) == ["habeas-corpus"]
    assert rejected == []


def test_rejects_a_definition_with_no_source():
    """The one that matters. A primer definition pasted in unchanged looks
    exactly like this.

    Asserts the *missing-field* rejection specifically, not just that the row
    was dropped: an empty source also fails the https check, so a looser
    assertion here passes even with the presence check deleted.
    """
    accepted, rejected = validate([row(definition_source="")])
    assert accepted == []
    assert rejected[0][1].startswith("missing ")


def test_rejects_a_non_https_source():
    accepted, _ = validate([row(definition_source="law.cornell.edu/wex/habeas_corpus")])
    assert accepted == []


def test_rejects_an_empty_definition():
    accepted, _ = validate([row(definition="   ")])
    assert accepted == []


# --- aliases feed the coverage query, so a malformed one changes the claim --

def test_rejects_unparseable_aliases():
    accepted, rejected = validate([row(aliases="TPS")])  # bare string, not JSON
    assert accepted == []
    assert "aliases" in rejected[0][1]


def test_rejects_aliases_that_are_not_a_list_of_strings():
    for bad in ('{"a": 1}', '[""]', "[123]", '["  "]'):
        accepted, _ = validate([row(aliases=bad)])
        assert accepted == [], f"{bad!r} should have been rejected"


def test_blank_aliases_cell_is_an_empty_list_not_an_error():
    accepted, _ = validate([row(aliases="")])
    assert accepted[0]["aliases"] == "[]"


def test_rejects_an_alias_claimed_by_another_term():
    """Two terms matching the same surface form would file one article under
    both, each page claiming coverage the other also claims."""
    accepted, rejected = validate([
        row(slug="temporary-protected-status", term="TPS", aliases='["TPS"]'),
        row(slug="transaction-privilege-tax", term="TPT", aliases='["tps"]'),
    ])
    assert slugs(accepted) == ["temporary-protected-status"]
    assert "already claimed" in rejected[0][1]


def test_a_term_may_reuse_its_own_alias_across_edits():
    accepted, _ = validate([row(aliases='["writ of habeas corpus", "habeas"]')])
    assert accepted[0]["aliases"] == '["writ of habeas corpus","habeas"]'


# --- definition_checked reaches asyncpg as a date, not a string ------------

def test_checked_date_is_parsed_to_a_date_object():
    """A `date` column binds by type in asyncpg. Passing the raw string is a
    DataError that aborts the whole executemany — every good row in the batch
    fails with the bad one."""
    from datetime import date

    accepted, _ = validate([row(definition_checked="2026-08-10")])
    assert accepted[0]["definition_checked"] == date(2026, 8, 10)


def test_rejects_a_malformed_checked_date():
    accepted, rejected = validate([row(definition_checked="Aug 10 2026")])
    assert accepted == []
    assert "YYYY-MM-DD" in rejected[0][1]


def test_blank_checked_date_is_none_not_an_error():
    accepted, _ = validate([row(definition_checked="")])
    assert accepted[0]["definition_checked"] is None


# --- slugs are URLs --------------------------------------------------------

def test_rejects_a_slug_that_is_not_url_safe():
    for bad in ("Habeas Corpus", "habeas_corpus", "-habeas", "habeas--corpus"):
        accepted, _ = validate([row(slug=bad)])
        assert accepted == [], f"{bad!r} should have been rejected"


def test_rejects_a_duplicate_slug():
    accepted, rejected = validate([row(), row(term="Habeas Corpus (dup)")])
    assert len(accepted) == 1
    assert "duplicate slug" in rejected[0][1]


# --- one bad row never takes the run down ----------------------------------

def test_a_bad_row_is_dropped_and_the_good_ones_still_seed():
    accepted, rejected = validate([
        row(slug="prior-restraint", definition_source=""),
        row(),
    ])
    assert slugs(accepted) == ["habeas-corpus"]
    assert len(rejected) == 1


# --- the shipped CSV must itself pass --------------------------------------

def test_the_committed_csv_has_no_rejected_rows():
    import csv

    with open(CSV_PATH, encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    accepted, rejected = validate(rows)
    assert rejected == [], f"data/term_profiles.csv has bad rows: {rejected}"
    assert len(accepted) == len(rows) > 0
