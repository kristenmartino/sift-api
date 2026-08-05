"""Tests for scripts/seed_entity_aliases.py — the ambiguity gate.

DB-free: `blocking_conflicts` is pure, so the catalog is an inline list of
(entity_type, canonical_id, lowercased_name) triples shaped exactly like
the one `main()` builds from the four profile tables.

What this guards: the check used to reject any alias appearing as a whole
word in a second profile name, which kept out the two highest-volume
curated aliases ("congress", "postal service") for collisions the linker
can now resolve itself. Relaxing it must not also let the person-name
collisions (#40's Kennedy / Miller / Collins) back in.
"""
from __future__ import annotations

import importlib.util
import pathlib
import sys

# Load the script as a module without invoking its main(). It's not in a
# package, so we import via spec.
SCRIPT = pathlib.Path(__file__).parent.parent / "scripts" / "seed_entity_aliases.py"
_spec = importlib.util.spec_from_file_location("seed_entity_aliases", SCRIPT)
assert _spec is not None and _spec.loader is not None
seed_entity_aliases = importlib.util.module_from_spec(_spec)
sys.modules["seed_entity_aliases"] = seed_entity_aliases
_spec.loader.exec_module(seed_entity_aliases)

blocking_conflicts = seed_entity_aliases.blocking_conflicts


# A slice of the real catalog, enough to exercise every branch.
CATALOG: list[tuple[str, str, str]] = [
    ("org", "united-states-congress", "united states congress"),
    ("org", "library-of-congress", "library of congress"),
    ("org", "united-states-postal-service", "united states postal service"),
    ("bill", "hr-3076-117", "postal service reform act of 2022"),
    ("org", "united-states-senate", "united states senate"),
    ("politician", "K000393", "john kennedy"),
    ("politician", "K000388", "joe kennedy"),
    ("politician", "EXEC-KENNEDY-RFK", "robert f. kennedy jr."),
    ("politician", "K000377", "patrick kennedy"),
    ("politician", "C001035", "susan collins"),
    ("politician", "C001093", "doug collins"),
    ("outlet", "new-york-times", "the new york times"),
    ("outlet", "los-angeles-times", "los angeles times"),
]


# ── the collisions longest-match-wins resolves ────────────────


def test_congress_is_accepted_despite_library_of_congress():
    """The motivating case. 'Congress' is not the head of 'Library of
    Congress' — that names a library — so the longer key owns the span."""
    assert blocking_conflicts(
        "congress", "org", "united-states-congress", CATALOG,
    ) == []


def test_postal_service_is_accepted_despite_the_reform_act():
    """Collides only with bill hr-3076-117, whose longer name wins."""
    assert blocking_conflicts(
        "postal service", "org", "united-states-postal-service", CATALOG,
    ) == []


def test_alias_matching_nothing_else_is_accepted():
    assert blocking_conflicts(
        "senate", "org", "united-states-senate", CATALOG,
    ) == []


def test_target_profile_is_never_its_own_conflict():
    """The alias always matches its own canonical name — that isn't ambiguity."""
    assert blocking_conflicts(
        "los angeles times", "outlet", "los-angeles-times", CATALOG,
    ) == []


# ── the collisions that must stay rejected ────────────────────


def test_person_surname_stays_rejected():
    """#40's failure. News copy writes 'Kennedy said' with no longer name
    present, so containment in 'John Kennedy' buys the linker nothing."""
    hits = blocking_conflicts("kennedy", "politician", "K000393", CATALOG)
    assert len(hits) == 3
    assert ("politician", "K000388") in hits


def test_person_surname_rejected_even_with_a_suffixed_full_name():
    """'Robert F. Kennedy Jr.' does not *end* with 'Kennedy', so a purely
    positional head test would call it resolvable. Politicians are excluded
    outright for exactly this reason."""
    hits = blocking_conflicts("kennedy", "org", "some-org", CATALOG)
    assert ("politician", "EXEC-KENNEDY-RFK") in hits


def test_two_way_person_collision_stays_rejected():
    """One colliding profile is still one too many when it's a person."""
    assert blocking_conflicts(
        "collins", "politician", "C001035", CATALOG,
    ) == [("politician", "C001093")]


def test_shared_head_noun_across_outlets_stays_rejected():
    """'Times' *is* the head of 'Los Angeles Times' — a bare 'the Times'
    has no longer span to lose to, so containment doesn't save it."""
    hits = blocking_conflicts("times", "outlet", "new-york-times", CATALOG)
    assert hits == [("outlet", "los-angeles-times")]


def test_alias_equal_to_another_profile_name_stays_rejected():
    """Nothing longer exists to win the span."""
    assert blocking_conflicts(
        "united states senate", "org", "some-other-org", CATALOG,
    ) == [("org", "united-states-senate")]


def test_whole_word_only():
    """A substring of a name is not a collision at all — the linker's own
    matcher is word-bounded, so 'congress' never fires on 'congressional'."""
    catalog = [("org", "cbo", "congressional budget office")]
    assert blocking_conflicts("congress", "org", "us-congress", catalog) == []
