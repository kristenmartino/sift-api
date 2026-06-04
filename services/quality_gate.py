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
- `background` is trimmed for clichés only — never for restatement (the audit
  found it appropriately novel), only on short paragraphs (a long paragraph with
  a stray cliché clause is kept), and terms are never touched (they're the gold).
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

_WORD_RE = re.compile(r"[A-Za-z][A-Za-z'’\-]*")

# Strings models sometimes emit to mean "no line" — treated as a drop.
_NULL_TOKENS = frozenset({"null", "none", "n/a", "na", "-", "—", "(none)", "[none]"})


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
    return lexical_novelty(line, reference) <= max_novelty


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

    Lighter touch than why_it_matters: clichés only. The audit found background
    appropriately novel, so the restatement backstop is NOT applied here — only
    vague-significance/editorial clichés are trimmed, and only on SHORT
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

    cliche = find_cliche(cleaned)
    if cliche and len(cleaned.split()) <= BACKGROUND_CLICHE_MAX_WORDS:
        return GateResult("", "cliche", cliche, novelty)

    return GateResult(cleaned, "ok", None, novelty)


def gate_background(background: str | None, *, title: str = "", summary: str = "") -> str:
    """Thin wrapper: cleaned background to store, or "" to drop the paragraph."""
    return evaluate_background(background, title=title, summary=summary).kept or ""
