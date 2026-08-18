"""The calibration harness's ground truth, pinned.

`scripts/eval_judge_calibration.py` exists to decide whether the faithfulness
judge can be trusted. Nothing decided whether IT could be trusted, and it could
not: plantability was read off the summary alone, so a broadcaster accused of
skipping an athlete introduction was planted as a `legal_safe` violation. There
is no legal matter there, the rubric's own "true if no legal matter is involved"
makes `true` correct, and the judge's right answer was recorded as a miss —
which is most of the difference between the reported "1/2, below the bar" and
the real 1/1.

A planted violation is a TEST CASE. An invalid test case does not report a bug
in the thing under test; it reports a bug in the test. These pin the three
properties that make a plant valid ground truth:

  1. the axis is actually engaged (the ARTICLE has a legal matter / attributes)
  2. the edit really is a violation (the article hedged, so removing the hedge
     over-characterizes)
  3. the edit changes ONLY that — no broken grammar or orphaned pronoun the
     judge could react to for an unrelated reason

Same doctrine as `tests/test_meta_suite.py` and the sibling repo's
`stryker.conf.json`: an assertion that cannot fail certifies nothing.
"""
from __future__ import annotations

import scripts.eval_judge_calibration as C

# Verbatim from data/eval/summarizer_corpus.jsonl — the case that broke it.
RTVE_ARTICLE = (
    "Spanish public broadcaster RTVE has been criticised after it appeared to "
    "skip the introduction of Israeli hurdler Omri Shiff during its coverage "
    "of the European Athletics Championships. RTVE said the omission was down "
    "to the complexity of a live broadcast and not a political decision."
)
RTVE_SUMMARY = (
    "Spanish broadcaster RTVE allegedly skipped introducing Israeli hurdler "
    "Omri Shiff during coverage of the European Athletics Championships."
)

COURT_ARTICLE = (
    "A federal appeals court dismissed as moot a challenge to the "
    "administration's use of the Alien Enemies Act to deport alleged gang "
    "members. The plaintiffs had argued the law did not apply."
)
COURT_SUMMARY = (
    "A federal appeals court dismissed a challenge to the use of the Alien "
    "Enemies Act to deport alleged gang members."
)


class TestLegalContext:
    def test_hedge_word_alone_is_not_a_legal_matter(self):
        """THE REGRESSION. 'allegedly' is a hedge, not a legal proceeding."""
        assert not C.has_legal_context(RTVE_ARTICLE)

    def test_rtve_summary_yields_no_legal_plant(self):
        assert C.plant_legal("t", RTVE_ARTICLE, RTVE_SUMMARY) == []

    def test_court_proceeding_is_a_legal_matter(self):
        assert C.has_legal_context(COURT_ARTICLE)

    def test_journalistic_investigation_is_not_a_legal_matter(self):
        """A BBC investigation is reporting; an FBI one is law."""
        assert not C.has_legal_context(
            "A BBC investigation found accounts selling advice to migrants.")

    def test_authority_investigation_is_a_legal_matter(self):
        assert C.has_legal_context(
            "The Federal Trade Commission opened an investigation into the vendor.")


class TestLegalPlants:
    def test_loud_plant_escalates_to_guilt(self):
        plants = C.plant_legal("t", COURT_ARTICLE, COURT_SUMMARY)
        loud = [p for p in plants if p.tier == "loud"]
        assert loud and "confirmed gang members" in loud[0].summary

    def test_quiet_plant_requires_the_article_to_hedge(self):
        """Deleting a hedge the source never used is not over-characterizing.

        The summary says "reportedly"; the article states it flat. Removing it
        leaves the summary no stronger than its source, so this is not a
        `legal_safe` violation and must not be planted as one.
        """
        article = "The state executed the prisoner by lethal injection after a court ruling."
        summary = "The state reportedly executed the prisoner after a court ruling."
        assert C.has_legal_context(article)
        assert [p for p in C.plant_legal("t", article, summary) if p.tier == "quiet"] == []

    def test_quiet_plant_fires_when_the_article_does_hedge(self):
        quiet = [p for p in C.plant_legal("t", COURT_ARTICLE, COURT_SUMMARY)
                 if p.tier == "quiet"]
        assert quiet
        assert "deport gang members" in quiet[0].summary
        assert "alleged" not in quiet[0].summary

    def test_every_plant_actually_differs_from_the_original(self):
        for p in C.plant_legal("t", COURT_ARTICLE, COURT_SUMMARY):
            assert p.summary.strip() != COURT_SUMMARY.strip()


class TestGrammarArtifacts:
    def test_article_agreement_repaired_after_deletion(self):
        """'an alleged ring' -> 'an ring' would cue the judge on grammar."""
        article = "Authorities charged 14 people in an alleged distribution ring; "\
                  "the court filing names them."
        summary = "Authorities charged 14 people in an alleged distribution ring."
        quiet = [p for p in C.plant_legal("t", article, summary) if p.tier == "quiet"]
        assert quiet
        assert "an distribution" not in quiet[0].summary
        assert "a distribution ring" in quiet[0].summary

    def test_fix_articles_both_directions(self):
        assert C._fix_articles("an large ring") == "a large ring"
        assert C._fix_articles("a alleged ring") == "an alleged ring"


class TestAttributionPlants:
    def test_requires_the_article_to_attribute_to_that_source(self):
        article = "The professor was found dead in south London on Friday."
        summary = "The professor was found dead; police say it is not suspicious."
        assert C.plant_attribution("t", article, summary) == []

    def test_fires_when_the_article_attributes(self):
        article = "Police said the death was unexpected but not suspicious."
        summary = "The professor was found dead; police say it is not suspicious."
        plants = C.plant_attribution("t", article, summary)
        assert plants
        assert all("police say" not in p.summary.lower() for p in plants)

    def test_stripping_must_not_orphan_a_pronoun(self):
        """Removing the subject leaves 'he'/'his' pointing at nothing.

        That is a broken sentence, not a flat assertion, and the judge could
        object to it for a reason that has nothing to do with attribution.
        """
        article = "President Trump said the cameras are under review, according to officials."
        summary = "President Trump said the cameras are under review and he will "\
                  "announce his position within weeks."
        # The guard must suppress the plant entirely. Asserting on the START of
        # the string was the first draft and it SURVIVED disabling the guard —
        # the orphaned pronoun is mid-sentence ("he will announce his"), so the
        # plant begins "The cameras..." and the check never looked at it. That
        # is the same can-it-fail defect this module exists to prevent, one
        # level up; the mutation run is what caught it.
        assert C.plant_attribution("t", article, summary) == []


class TestNegativeControl:
    def test_control_is_strictly_more_attributed(self):
        c = C.make_control("t", COURT_ARTICLE, COURT_SUMMARY, "Fox News")
        assert c.summary.startswith("According to Fox News, ")
        assert c.tier == "control"

    def test_control_preserves_the_claim(self):
        c = C.make_control("t", COURT_ARTICLE, COURT_SUMMARY, "Fox News")
        assert "alleged gang members" in c.summary


class TestPlantersAreDiscriminating:
    """The meta-check: would these planters notice if the gate were removed?"""

    def test_legal_gate_is_load_bearing(self, monkeypatch):
        """With the article gate stubbed true, RTVE plants again.

        If this test fails, the gate has stopped doing anything and the
        RTVE-class defect is back.
        """
        assert C.plant_legal("t", RTVE_ARTICLE, RTVE_SUMMARY) == []
        monkeypatch.setattr(C, "has_legal_context", lambda _a: True)
        assert C.plant_legal("t", RTVE_ARTICLE, RTVE_SUMMARY) != []
