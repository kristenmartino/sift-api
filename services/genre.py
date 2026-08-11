"""Opinion-vs-reported genre detection — ranking v2 stage 4.

The first hand-labeled ranking eval (data/eval/ranking_pairs.picks.txt,
sift-api#200) showed the clearest gap in the ranking's signal set: in half
of the labeler's overrules she rejected op-eds and horse-race commentary in
favor of reported events. No existing signal — importance, tone, civic
links — distinguishes genre, and the cross-spectrum bonus actively rewards
opinion roundups (op-eds across lanes trivially span L/C/R buckets).

Deterministic and precision-first: outlets declare opinion in their URL
paths (nypost.com/.../opinion/..., thehill.com/opinion/...,
theguardian.com/commentisfree/...) or title prefixes ("Opinion: ...").
Measured against 30 days of prod before shipping: 1,060 URL matches + ~50
title matches out of 53,442 feed articles (2%), every sampled match
genuinely opinion; politics pool is ~6% opinion. "Analysis:" is
deliberately NOT flagged — analysis is reported-adjacent, and a false
opinion flag buries real reporting (same asymmetry as false-grim).

The residual (opinion not declared in URL or title) is a known miss;
an LLM genre key on the context call is the follow-up if the residual
proves to matter. scripts/backfill_opinion.py mirrors these patterns in
SQL for the retroactive pass — keep the two in lockstep.
"""
from __future__ import annotations

import re

# Path segments outlets use to mark opinion sections. Matched as whole
# segments anywhere in the path, so nypost's /2026/07/31/opinion/... and
# thehill's /opinion/... both hit. commentisfree is The Guardian's.
_OPINION_PATH = re.compile(
    r"://[^/?#]+/(?:[^?#]*/)?(?:opinion|opinions|commentary|editorial|editorials|op-ed|commentisfree)(?:/|$|[?#])",
    re.IGNORECASE,
)

# Title prefixes: "Opinion: ...", "Editorial | ...", "Comment: ...".
_OPINION_TITLE = re.compile(r"^\s*(?:opinion|editorial|comment)\s*[:|]", re.IGNORECASE)


def detect_opinion(source_url: str | None, title: str | None) -> bool:
    """True when the outlet itself labels the piece as opinion."""
    if source_url and _OPINION_PATH.search(source_url):
        return True
    if title and _OPINION_TITLE.match(title):
        return True
    return False
