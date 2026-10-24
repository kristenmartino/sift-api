"""Pin live failures at the generation/poller boundaries, with retention controls."""
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from services import context_generator as cg, primer_generator as pg
from services import story_synthesizer as ss
from services.copy_rules import COPY_RULES
from services.quality_gate import gate_background, gate_framing, gate_summary, gate_why_it_matters
from services.index_alignment import AlignmentError
from services.story_synthesizer import synthesize_story
from services.summarizer import _build_prompt, _parse_summaries

URL = "https://example.com/one"
SUMMARY = "The central bank held its policy rate at 4.25%."
ARTICLE = {"source_url": URL, "title": "Bank holds rates", "summary": SUMMARY, "source_name": "A"}
TERM = {"term": "basis point", "definition": "One hundredth of a percentage point."}


def response(data):
    return SimpleNamespace(content=[SimpleNamespace(type="text", text=json.dumps(data))],
                           stop_reason="end_turn", usage=SimpleNamespace(input_tokens=10, output_tokens=10))


def batch_result(data):
    return [{"custom_id": "p0", "result": {"type": "succeeded", "message": {
        "content": [{"type": "text", "text": json.dumps(data)}]}}}]


class Pool:
    def __init__(self):
        self.fetchrow = AsyncMock(return_value={"metadata": {"p0": [URL]}})
        self.fetch = AsyncMock(return_value=[ARTICLE])
        self.execute = AsyncMock()


def test_committed_live_failures_do_not_return():
    path = Path(__file__).parents[1] / "data/eval/feed_copy_regressions.json"
    rows = json.loads(path.read_text())
    assert len(rows) == 5
    for row in rows:
        if row["field"] == "summary":
            actual = gate_summary(row["input"])
        else:
            actual = gate_framing(row["input"], source_text=row["source_text"])
        assert actual == row["expected"], row["id"]


@pytest.mark.asyncio
async def test_every_generator_includes_the_same_copy_standard():
    article = SimpleNamespace(**ARTICLE, raw_content=SUMMARY)
    assert COPY_RULES in _build_prompt([article])
    assert COPY_RULES in cg._build_context_prompt([ARTICLE])
    assert COPY_RULES in pg._PROMPT_HEADER.format(articles_text="example")
    client = AsyncMock()
    client.messages.create.return_value = response({"headline": "Bank holds rates", "summary": SUMMARY})
    await synthesize_story([ARTICLE, {**ARTICLE, "source_name": "B"}], client=client)
    assert COPY_RULES in client.messages.create.call_args.kwargs["messages"][0]["content"]


def test_article_parser_keeps_the_fact_and_drops_the_closer():
    parsed = _parse_summaries(json.dumps([{
        "i": 1, "s": SUMMARY + " This move highlights broader concerns about the economy.", "c": "business",
    }]), [SimpleNamespace(**ARTICLE, raw_content=SUMMARY)])
    assert parsed[URL]["summary"] == SUMMARY


def test_article_parser_rejects_a_population_not_in_the_source():
    source = SimpleNamespace(**ARTICLE, raw_content="Nigeria recorded its first case in the current Ebola outbreak.")
    with pytest.raises(AlignmentError, match="unsupported figure"):
        _parse_summaries(json.dumps([{"i": 1, "s": "Nigeria's outbreak affects a nation of 223 million.", "c": "health"}]), [source])


def test_spelled_out_source_figures_can_be_written_as_digits():
    source = SimpleNamespace(**ARTICLE, raw_content="The company launched four rockets.")
    parsed = _parse_summaries(json.dumps([{"i": 1, "s": "The company launched 4 rockets.", "c": "science"}]), [source])
    assert parsed[URL]["summary"] == "The company launched 4 rockets."


def test_automatic_context_cannot_add_a_new_population_or_repackage_the_summary():
    for line in ["Nigeria has 223 million people.", SUMMARY, "A new era for banking."]:
        parsed = cg._parse_context(json.dumps([{"i": 1, "c": line, "s": 4}]), [ARTICLE])
        assert parsed[URL]["context"] is None
        assert parsed[URL]["score"] == 4


@pytest.mark.asyncio
async def test_story_cannot_infer_an_election_date_from_the_event_name():
    client = AsyncMock()
    client.messages.create.return_value = response({
        "headline": "Talks planned after elections", "summary": "Talks are planned after the November 3 midterm elections.",
    })
    inputs = [{**ARTICLE, "title": "Talks after elections", "summary": "Talks are planned after midterm elections.", "source_name": n} for n in ["A", "B"]]
    result = await synthesize_story(inputs, client=client)
    assert result["_failed"] is True
    assert "November 3" not in result["summary"]


def test_a_paraphrased_angle_or_unspecified_location_is_not_a_source_passage():
    assert gate_framing("Explicitly states public risk assessment as low from disease experts.",
                        source_text="Experts say the public risk is low.") == ""
    assert gate_framing("The location is unspecified.", source_text="The location is unspecified.") == ""


def test_primer_does_not_invent_earlier_plans_or_a_population():
    article = {**ARTICLE, "title": "NASA updates lander plans", "summary": "NASA revised its lunar lander plans."}
    for bg in ["Earlier plans relied on a single lander.", "NASA employs 18000 people."]:
        assert pg._parse_primers(json.dumps([{"i": 1, "b": bg, "t": []}]), [article]) == {}


@pytest.mark.parametrize("line", [
    "The court ruled without addressing the constitutional challenge.",
    "The minister called the decision 'a wake-up call' for regulators.",
    "Researchers say transmission between people is unlikely.",
    "Raises concern about alleged Meta interference favoring frontrunner Bolsonaro.",
    "References Starshield systems and raises question of who ordered the Starlink slowdown.",
])
def test_specific_reported_details_and_attribution_survive(line):
    assert gate_framing(line, source_text=line, summary=SUMMARY) == line


@pytest.mark.parametrize("line", [
    "The development underscores broader tensions between the countries.",
    "This article explores the broader implications of the dispute.",
    "Raises serious questions about the future.",
    "A condition not mentioned in the other outlet's input.",
    SUMMARY,
    " ".join(["padding"] * 26),
])
def test_optional_story_notes_do_not_pad_or_repeat_the_summary(line):
    assert gate_framing(line, source_text=SUMMARY, summary=SUMMARY) == ""


def test_changed_numbers_are_not_mistaken_for_repeated_facts():
    new = "The central bank held its policy rate at 5.25%."
    assert gate_why_it_matters(new, title="", summary=SUMMARY) == new
    assert gate_framing(new, source_text=new, summary=SUMMARY) == new


def test_a_new_qualification_is_not_deleted_as_repetition():
    statement = "The ministry will fund the research trial."
    qualified = "The ministry may fund the research trial."
    assert gate_framing(qualified, source_text=qualified, summary=statement) == qualified


@pytest.mark.parametrize(("old", "new"), [("$100M", "$200M"), ("$1.2bn", "$1.8bn")])
def test_abbreviated_figures_are_not_mistaken_for_repeated_facts(old, new):
    reference = f"The company plans to invest {old} in the factory."
    detail = f"The company plans to invest {new} in the factory."
    assert gate_framing(detail, source_text=detail, summary=reference) == detail


def test_useful_financial_mechanism_is_not_a_generic_significance_line():
    bg = "Bond yields rise when investors demand higher returns, reflecting perceived risk and increasing government borrowing costs."
    assert gate_background(bg, title=ARTICLE["title"], summary=SUMMARY) == bg


@pytest.mark.asyncio
async def test_story_rows_keep_actual_sources_once_and_reject_invented_ones(monkeypatch):
    monkeypatch.setattr(ss, "judge_lines", AsyncMock(return_value=[{
        "id": "A", "judged": True, "restates": False, "adds_significance": True, "neutral_cliche_free": True,
    }]))
    client = AsyncMock()
    client.messages.create.return_value = response({
        "headline": "Officials reportedly plan talks, sources say", "summary": SUMMARY,
        "framings": [
            {"source_name": "A", "framing": SUMMARY, "tone": "critical"},
            {"source_name": "A", "framing": "The bank also changed its reserve requirements.", "tone": "analytical"},
            {"source_name": "Ghost outlet", "framing": "An invented angle.", "tone": "urgent"},
        ],
    })
    source_a = {**ARTICLE, "summary": SUMMARY + " The bank also changed its reserve requirements."}
    result = await synthesize_story([source_a, {**ARTICLE, "source_name": "B"}, source_a], client=client)
    assert result["headline"] == "Officials reportedly plan talks"
    assert result["framings"] == [
        {"source_name": "A", "framing": "The bank also changed its reserve requirements.", "tone": "analytical"},
        {"source_name": "B", "framing": "", "tone": "neutral"},
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["budget", "error", "unjudged", "repeated"])
async def test_optional_notes_are_withheld_when_review_cannot_approve_them(monkeypatch, failure):
    monkeypatch.setattr(ss, "check_budget", AsyncMock(return_value=SimpleNamespace(allowed=failure != "budget")))
    review = AsyncMock(return_value=[{"id": "A", "judged": failure != "unjudged", "restates": True,
                                    "adds_significance": False, "neutral_cliche_free": True}])
    if failure == "error":
        review.side_effect = RuntimeError("review unavailable")
    monkeypatch.setattr(ss, "judge_lines", review)
    result = {"headline": "Bank update", "summary": SUMMARY,
              "framings": [{"source_name": "A", "framing": "A real source detail.", "tone": "analytical"}]}
    await ss._review_framings(result, None)
    assert result["framings"] == [{"source_name": "A", "framing": "", "tone": "neutral"}]
    if failure == "budget":
        review.assert_not_awaited()


@pytest.mark.asyncio
async def test_explicitly_unresolved_cause_cannot_become_a_causal_headline():
    inputs = [{**ARTICLE, "source_name": n, "summary": "Investigators are studying whether the virus contributed to the death."} for n in ["A", "B"]]
    client = AsyncMock()
    client.messages.create.return_value = response({"headline": "First death linked to virus", "summary": "A patient died."})
    result = await synthesize_story(inputs, client=client)
    assert result["_failed"] is True


@pytest.mark.asyncio
async def test_scheduled_and_realtime_primers_have_identical_reference_checks(monkeypatch):
    pool = Pool()
    monkeypatch.setattr(pg, "get_pool", AsyncMock(return_value=pool))
    payload = [{"i": 1, "b": SUMMARY, "t": [TERM]}]
    realtime = pg._parse_primers(json.dumps(payload), [ARTICLE])[URL]
    await pg.process_primer_batch_results("batch", batch_result(payload))
    stored, url = pool.execute.call_args.args[1:]
    scheduled = json.loads(stored)
    assert url == URL
    assert scheduled["background"] == realtime["background"] == ""
    assert scheduled["terms"] == realtime["terms"] == [TERM]
    pool.fetch.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("missing", [True, False])
async def test_primer_does_not_publish_unchecked_copy_when_reference_is_missing(monkeypatch, missing):
    pool = Pool()
    if missing:
        pool.fetch.return_value = []
    else:
        pool.fetch.side_effect = RuntimeError("database unavailable")
    monkeypatch.setattr(pg, "get_pool", AsyncMock(return_value=pool))
    await pg.process_primer_batch_results("batch", batch_result([{"i": 1, "b": SUMMARY, "t": [TERM]}]))
    pool.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_context_without_reference_keeps_score_but_omits_unchecked_note(monkeypatch):
    pool = Pool()
    pool.fetch.side_effect = RuntimeError("database unavailable")
    monkeypatch.setattr(cg, "get_pool", AsyncMock(return_value=pool))
    await cg.process_context_batch_results("batch", batch_result([{"i": 1, "c": "An unsupported stake.", "s": 4}]))
    args = pool.execute.call_args.args
    assert args[1] is None
    assert args[2] == 4


def test_primer_does_not_repeat_term_definition_or_truncate_long_definitions():
    too_long = {"term": "example", "definition": " ".join(["word"] * 26)}
    result = pg._parse_primers(json.dumps([{"i": 1, "b": TERM["definition"], "t": [TERM, too_long]}]), [ARTICLE])
    assert result[URL]["background"] == ""
    assert result[URL]["terms"] == [TERM]
