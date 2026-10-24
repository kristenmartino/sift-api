"""Deterministic quality gate for AI-generated card copy (sift-api#90).

The 500-article audit (kristenmartino/sift#150) found the per-article
`why_it_matters` line fails two ways: it either *restates* the summary
(adds words, no facts) or hand-waves with vague-significance clichés
("raises serious questions…"). `context_primer.background` performs well but
carries a ~18% cliché rate worth trimming.

This module is the cheap, deterministic half of the fix: pure-Python pattern
checks that drop the obvious failures at write time, for free, in the same
poller callback that stores generated content. The rubric in the generation
prompt is the primary semantic gate; the LLM judge (services/judge.py) is the
offline measurement. This sits between them — catching the clichés the prompt
slips on without paying for a second model call.

Design choices grounded in the audit:
- Clichés, not lexical overlap, are the workhorse. Both live failure examples
  (cop-fired ~36% novel, Kepner ~67% novel) sit ABOVE any safe lexical-overlap
  threshold — a lexical gate misses them, but each contains an unmistakable
  cliché. So `find_cliche` does the heavy lifting; `is_near_restatement` is a
  conservative backstop for near-verbatim restatement only (the ~1% tail).
- Patterns are HIGH precision: phrases that are almost always filler, so a real
  line is rarely false-dropped. Subtler paraphrase/fluff is the judge's job.
- `background` is checked against its article and term definitions for near
  repetition. Cliché-only drops apply to short paragraphs; a longer useful
  explanation is not discarded for a stray cliché clause.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

# Backstop threshold: drop a why_it_matters line whose content words are <=20%
# novel vs. title+summary, i.e. >=80% lexical overlap — near-verbatim only.
# Deliberately strict so this never fires on a genuine, differently-worded stake.
NEAR_RESTATEMENT_MAX_NOVELTY = 0.20

# Background is a multi-sentence paragraph, not a one-liner. A stray cliché
# clause ("…raising questions about <specific tension>") inside an otherwise
# informative paragraph is usually legitimate, and blanking the whole paragraph
# to kill it destroys real context (the sift#150 audit validated background as
# good). So the cliché drop applies to background ONLY when the paragraph is
# short enough that the cliché is effectively its entire content. Longer
# paragraphs are kept even if they contain a flagged phrase.
BACKGROUND_CLICHE_MAX_WORDS = 25

# Small curated English stopword set. Intentionally not exhaustive — the goal is
# a stable content-word signal for novelty, not perfect linguistics.
_STOPWORDS = frozenset({
    "a", "an", "the", "and", "or", "but", "if", "of", "to", "in", "on", "for",
    "with", "as", "by", "at", "from", "into", "over", "after", "before",
    "is", "are", "was", "were", "be", "been", "being", "am", "it", "its",
    "this", "that", "these", "those", "they", "them", "their", "he", "she",
    "his", "her", "him", "we", "us", "our", "you", "your", "i", "me", "my",
    "has", "have", "had", "do", "does", "did", "will", "would", "can", "could",
    "should", "may", "might", "must", "not", "no", "so", "than", "then",
    "there", "here", "when", "where", "who", "what", "which", "how", "why",
    "about", "up", "out", "off", "down", "more", "most", "some", "any", "all",
    "new", "now", "also", "just", "only", "both", "each", "other", "such",
    "s", "t",
})

# Cliché / vague-significance / editorial tells. HIGH precision: each phrase is
# almost always filler in a one-line "why it matters". Subtler cases (paraphrased
# restatement, soft editorial color that dodges these phrasings) are left to the
# LLM judge. Add to this list as the eval surfaces new repeat offenders.
_CLICHE_SOURCES = [
    # vague-significance hand-waving (the dominant audit failure)
    r"rais\w*\s+(?:serious\s+|new\s+|fresh\s+|important\s+|major\s+|tough\s+)?questions?",
    r"rais\w*\s+(?:serious\s+|new\s+|fresh\s+|deep\s+|grave\s+)?concerns?",
    r"worth\s+(?:examining|watching|noting|considering|a\s+look)",
    r"remains?\s+to\s+be\s+seen",
    r"only\s+time\s+will\s+tell",
    r"begs?\s+the\s+question",
    r"the\s+latest\s+(?:sign|example|reminder)\s+(?:that|of)",
    # significance-by-assertion
    r"(?:a|the)\s+(?:major\s+|real\s+|potential\s+)?turning\s+point",
    r"wake[\s-]?up\s+call",
    r"(?:stark|grim|sobering)\s+reminder",
    r"serv\w+\s+as\s+a\s+(?:stark\s+|grim\s+|sobering\s+)?reminder",
    r"underscor\w+\s+the\s+(?:importance|need|urgency|gravity|scale)",
    r"highlight\w*\s+the\s+(?:importance|need|urgency|gravity)",
    r"signal\w*\s+a\s+(?:major\s+|significant\s+|seismic\s+|broader\s+)?(?:shift|change|departure)",
    r"(?:significant|profound|sweeping|seismic|far[\s-]?reaching)\s+implications",
    r"could\s+have\s+(?:major|significant|serious|profound|lasting|far[\s-]?reaching)\s+"
    r"(?:consequences|implications|effects|ramifications)",
    r"far[\s-]?reaching\s+(?:consequences|effects|ramifications)",
    r"ripple\s+effects?",
    # speculation / drama
    r"could\s+finally\b",
    r"send\w*\s+a\s+(?:clear\s+|strong\s+|powerful\s+|chilling\s+)?(?:message|signal)",
    r"the\s+stakes\s+(?:could\s+not|couldn'?t|have\s+never|had\s+never)\s+\w*\s*be\w*\s+higher",
    r"now\s+more\s+than\s+ever",
    r"game[\s-]?changer",
    r"sound\w*\s+the\s+alarm",
    r"a\s+growing\s+chorus",
    r"all\s+eyes\s+(?:are\s+)?on\b",
    r"capture\w*\s+(?:the\s+)?(?:nation|public|world|country)['’]?s?\s+attention",
    # editorial color / superlatives (the high-novelty fluff failure)
    r"shock\w+\s+(?:the\s+)?(?:nation|world|public|community|industry)",
    r"haunt\w+\s+(?:investigators|detectives|residents|families|the\s+\w+)",
    r"finally\s+ha\w+\s+hope",
    r"most\s+(?:tortured|beloved|hated|notorious|infamous|storied|cursed)\b",
]

_CLICHE_RE = [re.compile(p, re.IGNORECASE) for p in _CLICHE_SOURCES]

# What the model says instead of a summary when the RSS entry has no body
# (sift-api#118). Each phrase declares the ARTICLE unsummarizable — deliberately
# not looser. "may not provide sufficient votes" is a real summary and appears
# in prod 97 times; a pattern like `not provide sufficient` would eat it.
_REFUSAL_SOURCES = [
    r"\bunable to summarize\b",
    r"\bunable to provide (?:a |an accurate )?summary\b",
    r"\bcannot be summarized\b",
    r"\bcannot summarize\b",
    r"\bsummary (?:is )?(?:not available|unavailable|cannot be (?:determined|provided|generated))\b",
    r"\b(?:no|insufficient|incomplete) (?:substantive )?(?:article )?content "
    r"(?:was |is |been )?(?:provided|available|present)\b",
    r"\binsufficient content\b",
    r"\b(?:no|without|lacking|lacks) substantive content\b",
    r"\bcontent not provided\b",
    r"\bno content (?:to summarize|provided)\b",
    r"\b(?:article |the )?content (?:is|appears) (?:to be )?incomplete\b",
    r"\barticle appears incomplete\b",
    r"\bincomplete article content\b",
    r"\bprevents? summarization\b",
    r"\binsufficient (?:information|detail) to summarize\b",
    r"\b(?:without|lacks) sufficient content\b",
    r"\bdoes not contain (?:sufficient|substantive|coherent)\b",
    r"\bno (?:article )?text (?:was )?provided\b",
]
_REFUSAL_RE = [re.compile(p, re.IGNORECASE) for p in _REFUSAL_SOURCES]

# A summary shorter than this after the apologies are removed is not a summary.
# Deliberately low: real summaries run 1-2 sentences, and the goal is to drop
# leftovers like "No content." rather than to enforce a length policy.
MIN_SUMMARY_WORDS = 10

_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+")

_WORD_RE = re.compile(r"[A-Za-z][A-Za-z'’\-]*")
_NUMBER_RE = re.compile(r"\b\d+(?:[.,]\d+)*(?:%|[KMBT]\b)?", re.IGNORECASE)
_QUALIFIER_RE = re.compile(
    r"\b(?:not|no|never|may|might|could|alleged|allegedly|reportedly|"
    r"accused|suspected|proposed|proposal)\b", re.IGNORECASE,
)

# Strings models sometimes emit to mean "no line" — treated as a drop.
_NULL_TOKENS = frozenset({"null", "none", "n/a", "na", "-", "—", "(none)", "[none]"})

# Attribution verbs do not turn an otherwise repeated fact into an added stake.
# Keep speaker nouns, names, numbers, and qualifications out of this set.
_ATTRIBUTION_WORDS = frozenset({
    "according", "assess", "assesses", "assessed", "assessment",
    "believe", "believes", "believing", "belief",
    "say", "says", "said", "state", "states", "stated",
    "statement", "statements", "report", "reports", "reported", "reporting",
})


def _clean(text: str | None) -> str:
    """Trim whitespace and a single layer of wrapping quotes."""
    if not text:
        return ""
    t = " ".join(str(text).split())
    if len(t) >= 2 and t[0] in "\"'“‘" and t[-1] in "\"'”’":
        t = t[1:-1].strip()
    return t


def _content_words(text: str) -> set[str]:
    """Lowercased content words (stopwords + 1-char tokens dropped)."""
    return {
        w
        for w in (m.group(0).lower() for m in _WORD_RE.finditer(text or ""))
        if len(w) > 1 and w not in _STOPWORDS
    }


def lexical_novelty(line: str, reference: str) -> float:
    """Fraction of `line`'s content words NOT present in `reference`.

    This reproduces the sift#150 audit's lexical-novelty proxy so the eval can
    report an audit-comparable column. 1.0 = all new words, 0.0 = pure
    restatement (or no content words). NOT used as the runtime gate — kept here
    because the eval and the near-restatement backstop both need it.
    """
    line_words = _content_words(line)
    if not line_words:
        return 0.0
    ref_words = _content_words(reference)
    return len(line_words - ref_words) / len(line_words)


def find_cliche(line: str) -> str | None:
    """Return the first matched cliché substring, or None if the line is clean."""
    for rx in _CLICHE_RE:
        m = rx.search(line or "")
        if m:
            return m.group(0)
    return None


def find_refusal(text: str) -> str | None:
    """Return the first matched refusal phrase, or None. See gate_summary."""
    for rx in _REFUSAL_RE:
        m = rx.search(text or "")
        if m:
            return m.group(0)
    return None


def gate_summary(summary: str | None) -> str:
    """Strip generic significance closers and the model's own apologies.

    When an article's RSS entry carries no usable body, Haiku answers with a
    refusal rather than a summary — "Insufficient content provided to
    summarize." — and it was stored and shown to readers verbatim. 109 rows
    over 30 days of prod (0.2%), concentrated in feeds whose RSS is
    headline-only.

    Sentence-level, not all-or-nothing, because the failure is often MIXED. A
    real case from prod:

        "Treasury Secretary Scott Bessent said in a Fox News interview that
         President Trump predicted the Democratic Party's shift toward
         democratic socialism as its center of gravity. The article's content
         appears incomplete and does not provide full context."

    The first sentence is the summary; the second is the apology. Dropping the
    whole thing to kill the apology would throw away real reporting — the same
    reasoning that makes gate_background spare long paragraphs.

    What survives must still be a summary, so a remainder under
    MIN_SUMMARY_WORDS is blanked entirely. An empty summary is not a blank
    card: every feed query in sift/lib/db.ts filters
    `summary IS NOT NULL AND summary != ''`, so the article simply stops being
    served — which is the right outcome for an article nobody could summarize.

    Patterns are high precision by design, matching this module's existing
    posture. "…even a simple majority may not provide sufficient votes" is a
    real summary and must not trip: phrases here name the ARTICLE as
    unsummarizable rather than merely using the words.
    """
    text = (summary or "").strip()
    sentences = _SENTENCE_SPLIT_RE.split(text)
    if any(_is_generic_summary_closer(sentence) for sentence in sentences):
        text = " ".join(
            sentence for sentence in sentences
            if not _is_generic_summary_closer(sentence)
        ).strip()
    if not text or not find_refusal(text):
        # Refusal handling is inert unless the model actually apologized:
        # MIN_SUMMARY_WORDS must never touch a summary that was fine, or a
        # terse-but-correct one ("The Fed held rates steady at 4.25%.") would
        # be destroyed by a gate aimed at something else entirely.
        return text

    kept = [s for s in _SENTENCE_SPLIT_RE.split(text) if s.strip() and not find_refusal(s)]
    remainder = " ".join(kept).strip()
    if len(remainder.split()) < MIN_SUMMARY_WORDS:
        return ""
    return remainder


_GENERIC_CLOSER_RE = re.compile(
    r"^(?:The|This) (?:development|move|incident|case|alert|warning|dispute|decision|"
    r"proposal|action|visit|surge|threat|finding|story|initiative|policy|troop movements|"
    r"disaster|discovery|event) "
    r"(?:reflects?|signals?|highlights?|underscores?|marks?|raises?|adds? to)\b"
    r".*\b(?:tensions?|concerns?|importance|significance|questions?|"
    r"challenges?|debate|notable event|risks?|understanding)\b",
    re.IGNORECASE,
)
_CONCRETE_CLOSER_RE = re.compile(
    r"\d|[\"“”]|(?:^|\s)['‘][^'’]+['’](?:[.,!?]|\s|$)|"
    r"\b(?:said|says|according to|warned|found|ruled|"
    r"raised by|cited by|voiced by|expressed by|citing)\b",
    re.IGNORECASE,
)
_ARTICLE_OMISSION_RE = re.compile(
    r"\b(?:the|this) (?:article|story|summary|coverage|headline|outlet) "
    r"(?:does not|doesn't|fails to) (?:mention|specify|address|explain|detail|provide)\b",
    re.IGNORECASE,
)
_INPUT_OMISSION_RE = re.compile(
    r"\b(?:not (?:mentioned|specified|addressed|explained|detailed|provided)|absent) "
    r"(?:in|from|by)\b.*\b(?:input|summary|excerpt|coverage|outlet)\b",
    re.IGNORECASE,
)
_META_PADDING_RE = re.compile(
    r"^(?:The|This) (?:article|story|piece|report|coverage) "
    r"(?:explores|discusses|examines|highlights|focuses on) "
    r"(?:the )?(?:broader|wider|complex|evolving|significant)\b",
    re.IGNORECASE,
)
_GENERIC_NOTE_RE = re.compile(
    r"^(?:raises?|highlights?|underscores?|reflects?)\b.*"
    r"\b(?:broader|wider|importance|significance|the future)\b", re.IGNORECASE,
)


def _is_generic_summary_closer(sentence: str) -> bool:
    """Catch standalone significance padding, preserving quoted/attributed detail.

    Deliberately narrow: this does not delete any sentence merely for using
    'concerns' or 'questions'. The generic subject, evaluative verb and abstract
    conclusion must all be present, with no figure, quote or attributed finding.
    """
    return bool(
        (_GENERIC_CLOSER_RE.search(sentence.strip())
         or _ARTICLE_OMISSION_RE.match(sentence.strip())
         or _META_PADDING_RE.match(sentence.strip()))
        and not _CONCRETE_CLOSER_RE.search(sentence)
    )


def clean_generated_headline(headline: str) -> str:
    """Remove only a redundant generic attribution, retaining 'reportedly'."""
    text = headline.strip()
    if re.search(r"\breportedly\b", text, re.IGNORECASE):
        text = re.sub(
            r",?\s+(?:sources? say|according to sources)\.?$", "", text,
            flags=re.IGNORECASE,
        )
    return text


_COVERAGE_OMISSION_RE = re.compile(
    r"\b(?:does not|doesn't|fails to) (?:mention|specify|address|explain|detail|provide)\b"
    r"|\bwithout (?:mentioning|specifying|addressing|explaining|detailing)\b"
    r"|\b(?:omits?|omitting)\b",
    re.IGNORECASE,
)


def gate_framing(framing: str | None, *, source_text: str, summary: str = "") -> str:
    """An excerpt's silence is not evidence that an outlet omitted something.

    An explicitly reported omission (e.g. a court ruled without addressing a
    question) may survive; it must appear in the supplied source text too.
    """
    text = framing.strip() if isinstance(framing, str) else ""
    if len(text.split()) > 25:
        return ""  # Omit, never truncate a qualification or attribution.
    # Generated summaries can themselves contain this meta-claim. Repeating
    # it upstream does not turn excerpt silence into a verified omission.
    if _ARTICLE_OMISSION_RE.search(text) or _INPUT_OMISSION_RE.search(text):
        return ""
    omissions = list(_COVERAGE_OMISSION_RE.finditer(text))
    source_lower = source_text.lower()
    if any(match.group(0).lower() not in source_lower for match in omissions):
        return ""
    if _is_generic_summary_closer(text):
        return ""
    # A reported concern/question can itself be a real source detail. Match
    # standalone abstract observations, not any use of 'raises concerns'.
    if _GENERIC_NOTE_RE.search(text) and not _CONCRETE_CLOSER_RE.search(text):
        return ""
    if summary and is_near_restatement(text, summary):
        return ""
    return text


def is_near_restatement(
    line: str,
    reference: str,
    max_novelty: float = NEAR_RESTATEMENT_MAX_NOVELTY,
) -> bool:
    """True when `line` is a near-verbatim restatement of `reference`.

    Conservative backstop: only fires on very low novelty (near-duplicate). The
    issue is explicit that lexical overlap is too weak to be THE gate — this only
    catches the egregious tail the prompt+cliché checks would otherwise miss.
    """
    # A changed figure is a different fact, even if every word matches.
    if {n.lower() for n in _NUMBER_RE.findall(line)} - {n.lower() for n in _NUMBER_RE.findall(reference)}:
        return False
    if {q.lower() for q in _QUALIFIER_RE.findall(line)} - {q.lower() for q in _QUALIFIER_RE.findall(reference)}:
        return False
    if lexical_novelty(line, reference) <= max_novelty:
        return True
    # "Advisers assess ... according to the administration's stated belief"
    # can pass a word-overlap check while adding only attribution scaffolding.
    line_words = {w.removesuffix("'s").removesuffix("’s") for w in _content_words(line)}
    reference_words = {w.removesuffix("'s").removesuffix("’s") for w in _content_words(reference)}
    return (line_words - reference_words) <= _ATTRIBUTION_WORDS


@dataclass(frozen=True)
class GateResult:
    """Outcome of gating one generated line."""

    kept: str | None       # cleaned line to store, or None to drop (store NULL)
    reason: str            # "ok" | "empty" | "cliche" | "restatement"
    cliche: str | None     # the matched cliché phrase, when reason == "cliche"
    novelty: float         # lexical novelty vs. reference (for eval/telemetry)

    @property
    def dropped(self) -> bool:
        # Truthy for both drop shapes: why_it_matters drops to None, background
        # drops to "" (blank the paragraph, keep terms). A kept line is truthy.
        return not self.kept


def evaluate_why_it_matters(line: str | None, *, title: str, summary: str) -> GateResult:
    """Deterministic verdict for a why_it_matters line.

    Drops (kept=None) when the line is empty/null-ish, contains a cliché, or is a
    near-verbatim restatement of title+summary. Otherwise returns the cleaned
    line. Null-over-filler: an absent line renders nothing on the card, which is
    the desired outcome when there's no real, neutral, verifiable stake.
    """
    reference = f"{title or ''} {summary or ''}"
    cleaned = _clean(line)
    novelty = lexical_novelty(cleaned, reference)

    if not cleaned or cleaned.lower() in _NULL_TOKENS:
        return GateResult(None, "empty", None, novelty)

    cliche = find_cliche(cleaned)
    if cliche:
        return GateResult(None, "cliche", cliche, novelty)

    if is_near_restatement(cleaned, reference):
        return GateResult(None, "restatement", None, novelty)

    return GateResult(cleaned, "ok", None, novelty)


def gate_why_it_matters(line: str | None, *, title: str, summary: str) -> str | None:
    """Thin wrapper: cleaned line to store, or None to drop (store NULL)."""
    return evaluate_why_it_matters(line, title=title, summary=summary).kept


def evaluate_background(background: str | None, *, title: str = "", summary: str = "") -> GateResult:
    """Deterministic verdict for a context_primer background paragraph.

    Trim near-verbatim repetition when the article reference is available.
    Vague-significance/editorial clichés are removed only on SHORT
    paragraphs (see BACKGROUND_CLICHE_MAX_WORDS): a long informative paragraph
    with a stray cliché clause is kept, since blanking it would destroy real
    context. Caller keeps `terms` regardless; an empty background just hides the
    paragraph.
    """
    reference = f"{title or ''} {summary or ''}"
    cleaned = _clean(background)
    novelty = lexical_novelty(cleaned, reference)

    if not cleaned:
        return GateResult("", "empty", None, novelty)

    if reference.strip() and is_near_restatement(cleaned, reference):
        return GateResult("", "restatement", None, novelty)

    cliche = find_cliche(cleaned)
    if cliche and len(cleaned.split()) <= BACKGROUND_CLICHE_MAX_WORDS:
        return GateResult("", "cliche", cliche, novelty)

    return GateResult(cleaned, "ok", None, novelty)


def gate_background(background: str | None, *, title: str = "", summary: str = "") -> str:
    """Thin wrapper: cleaned background to store, or "" to drop the paragraph."""
    return evaluate_background(background, title=title, summary=summary).kept or ""
