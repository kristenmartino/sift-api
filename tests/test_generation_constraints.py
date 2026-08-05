"""Every generation prompt carries the people-and-legal-matters constraints.

B2 in `sift/docs/LAUNCH_DECISION_MEMO.md` §5. `sift/docs/OPERATING_CONTEXT.md`
§5 forbids a claim about a living person without a citation to the primary
record, and these three prompts are the only places the product writes prose
about named people.

This test exists because the constraint was added to `summarizer` and
`context_generator` and silently missed on `primer_generator` — a gap nobody
noticed for weeks, because nothing asserted it. Prompts are strings; a string
that loses a paragraph fails nothing on its own.

Substring assertions are deliberately coarse. The point is that the paragraph
is present in every generator, not that its wording is frozen — reword freely,
and if a phrase here becomes wrong, update the constant rather than deleting
the case.
"""
from __future__ import annotations

import pytest

from services.context_generator import _build_context_prompt
from services.primer_generator import _PROMPT_HEADER as PRIMER_PROMPT
from services.summarizer import _build_prompt as build_summarizer_prompt

BATCH = [{
    "source_url": "https://example.com/1",
    "title": "State attorney general opens inquiry into a utility's billing",
    "summary": "The attorney general said the office had opened an inquiry.",
}]


class _Article:
    """Minimal stand-in for RSSArticle — summarizer only reads these fields."""

    def __init__(self) -> None:
        self.title = BATCH[0]["title"]
        self.summary = BATCH[0]["summary"]
        self.raw_content = BATCH[0]["summary"]
        self.source_name = "Example News"
        self.source_url = BATCH[0]["source_url"]
        self.image_url = None
        self.published_date = None


def _prompts() -> dict[str, str]:
    return {
        "summarizer": build_summarizer_prompt([_Article()]),
        "context_generator": _build_context_prompt(BATCH),
        # The primer prompt is a template; `.format` needs the articles slot.
        "primer_generator": PRIMER_PROMPT.format(articles_text="(articles)"),
    }


# The claim ladder is the load-bearing one: it is what stops "charged" being
# rendered as "guilty" about a named, living, non-public figure.
REQUIRED = [
    ('"Charged" is not "guilty."', "the legal-outcome ladder"),
    ("An accusation is not a fact.", "accusation/fact distinction"),
    ("Attribute contested claims to whoever made them", "source attribution"),
    ("A campaign contribution is not an endorsement.", "civic non-inference"),
]


@pytest.mark.parametrize("name", ["summarizer", "context_generator", "primer_generator"])
@pytest.mark.parametrize(("needle", "label"), REQUIRED, ids=[r[1] for r in REQUIRED])
def test_prompt_carries_constraint(name: str, needle: str, label: str) -> None:
    prompt = _prompts()[name]
    assert needle in prompt, (
        f"{name} prompt is missing {label}. Every generator that writes prose "
        f"about named people needs the full block — see summarizer.py."
    )


@pytest.mark.parametrize("name", ["summarizer", "context_generator", "primer_generator"])
def test_constraint_block_declares_itself_overriding(name: str) -> None:
    """The block must outrank the instructions above it.

    Each of these prompts pushes hard for output — a punchy stake, a useful
    primer. Without the override the model has to weigh "be useful" against
    "be careful", and the constraint has to win.
    """
    assert "override everything above" in _prompts()[name]


def test_primer_may_add_background_but_not_about_people() -> None:
    """The primer's licence is narrower than it looks.

    Unlike the other two, the primer is *supposed* to add material the article
    does not contain — that is the whole feature. So it cannot carry the
    summarizer's flat "use only what the article states", and the constraint
    has to draw the line somewhere else: institutions and procedure yes,
    living people no. If that carve-out is ever dropped, the block silently
    stops binding the primer's main risk.
    """
    prompt = _prompts()["primer_generator"]
    assert "well-established public record" in prompt
    assert "state of mind of a living person" in prompt
    assert "about a person rather than about a system" in prompt
