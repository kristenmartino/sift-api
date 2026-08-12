"""Funding edges from IRS 990 filings — parse, and check the join key.

Sift's org dossiers carry facts *about* one organization. This module builds
edges *between* organizations: who granted money to whom (Schedule I Part II)
and who declares whom a related tax-exempt org (Schedule R Part II). Both are
filed by the payer/declarer itself, both carry the counterparty's EIN, and
both are open to public inspection.

Why the EIN alone is not enough
───────────────────────────────
An EIN is a join key, not a truth: it is typed by the filer and filers make
mistakes. In the first ten-org pull (2026-08-11), Brookings' Schedule I listed
"Urban League of Louisiana" under EIN 52-0880375 — which belongs to The Urban
Institute. Joining on that EIN alone would have produced a confidently wrong,
fully "cited" edge between two unrelated organizations.

So every edge carries a verdict from `ein_name_agrees`, computed at ingest by
comparing the name **as filed** against the IRS's own name for that EIN (from
the annual index CSV, ~717k EINs). The verdict is stored, not just logged, and
only `AGREES` edges are publishable — the same posture as `publishFloor` in the
sibling repo: a large catalog, a smaller advertised set.

Deliberately three outcomes, not two. `REVIEW` does not mean "wrong": Harvard
Law School filed under President and Fellows of Harvard College is a legitimate
sub-unit, while Urban League under Urban Institute is an error, and no string
comparison can tell them apart. It means "a human has not confirmed this yet."
"""

from __future__ import annotations

import csv
import difflib
import logging
import re
from collections import defaultdict
from dataclasses import dataclass
from enum import Enum
from typing import Iterable
import xml.etree.ElementTree as ET

logger = logging.getLogger("sift-api.funding_edges")


class NameVerdict(str, Enum):
    """Whether an edge's EIN and its filed name tell the same story."""

    AGREES = "agrees"          # publishable
    REVIEW = "review"          # held back pending a human look
    EIN_ABSENT = "ein_absent"  # EIN not in the IRS index (non-filer, LLC, govt)


#: Legal-form noise that carries no identity. Deliberately short: stripping
#: meaningful words ("institute", "foundation") collapses distinct orgs onto
#: each other. An early version of this check dropped "the" and "institute",
#: which reduced "The Urban Institute" to the single token "urban" and scored
#: "Urban League of Louisiana" as a *perfect* match — the check certified the
#: exact defect it was built to catch. Keep this list to legal suffixes only.
_LEGAL_NOISE = re.compile(
    r"\b(?:THE|INC|INCORPORATED|CORP|CORPORATION|CO|LLC|L L C|LLP|LP|LTD|PLLC|PC)\b"
)

#: Below this many significant tokens, containment is too weak to trust:
#: "Harvard" inside "Harvard Law School" should not auto-approve.
_MIN_CONTAINMENT_TOKENS = 2

#: Similarity at or above this counts as the same name.
#:
#: Calibrated against the 121 edges of the 2026-08-11 pull rather than picked.
#: Scoring every edge against the IRS's name for its EIN produced a clean gap:
#:
#:   0.35  Urban League of Louisiana  vs  The Urban Institute   <- real defect
#:   0.38  Harvard Law School         vs  President and Fellows of Harvard College
#:   0.44  Claremont Institute        vs  The Claremont Institute for the Study of…
#:   ────────────────────────────── gap ──────────────────────────────
#:   0.65  Prevent Child Abuse Virginia DBA Families Forward
#:   0.76  Feds for Freedom           vs  Feds 4 Med Freedom Inc
#:   0.83  American Assoc of Pro-Life OB&G  vs  Association of Pro-Life OB and G
#:   0.87+ abbreviations, word order, apostrophes — 99 edges at 0.87 or above
#:
#: Everything below 0.65 is a different organization or a genuinely ambiguous
#: sub-unit; everything at 0.65 and above is the same organization spelled
#: differently. 0.60 sits in the gap with margin on both sides. An earlier
#: 0.85 held eight legitimate edges for no benefit.
#:
#: Bias stays toward holding: shipping a wrong edge between two real
#: organizations costs credibility, holding a right one costs a glance.
_SIMILARITY_FLOOR = 0.60


def normalize_org_name(name: str | None) -> str:
    """Uppercase, strip punctuation and legal-form suffixes, collapse spaces."""
    if not name:
        return ""
    s = re.sub(r"[^A-Za-z0-9 ]", " ", name).upper()
    s = _LEGAL_NOISE.sub(" ", s)
    return " ".join(s.split())


def _contains_at_token_boundary(haystack: str, needle: str) -> bool:
    """True when `needle` appears in `haystack` as whole words."""
    if not needle or not haystack:
        return False
    return re.search(rf"(?:^|\s){re.escape(needle)}(?:\s|$)", haystack) is not None


def names_agree(filed_name: str | None, official_name: str | None) -> bool:
    """Whether a filed counterparty name matches the IRS's name for that EIN.

    Accepts three shapes seen in real filings:
      - the same name (exactly, or modulo punctuation and legal suffixes)
      - a short form of the official name ("Claremont Institute" for
        "The Claremont Institute for the Study of Statesmanship & Political
        Philosophy") — containment at a token boundary
      - near-identical strings (ampersands, abbreviations, word order)

    Rejects everything else, including plausible-looking near-misses like
    "Urban League of Louisiana" against "The Urban Institute".
    """
    a, b = normalize_org_name(filed_name), normalize_org_name(official_name)
    if not a or not b:
        return False
    if a == b:
        return True
    shorter, longer = (a, b) if len(a) <= len(b) else (b, a)
    if (
        len(shorter.split()) >= _MIN_CONTAINMENT_TOKENS
        and _contains_at_token_boundary(longer, shorter)
    ):
        return True
    return difflib.SequenceMatcher(None, a, b).ratio() >= _SIMILARITY_FLOOR


def ein_name_agrees(
    filed_name: str | None,
    ein: str | None,
    ein_index: dict[str, set[str]],
) -> tuple[NameVerdict, str | None]:
    """Classify one edge's (name, EIN) pair against the IRS index.

    Returns the verdict and the best-matching official name (or the first
    known one when nothing matches, so a reviewer sees what the IRS thinks
    that EIN is). `ein_index` comes from `load_ein_index`.
    """
    if not ein or not re.fullmatch(r"\d{9}", ein):
        return NameVerdict.REVIEW, None
    official = ein_index.get(ein)
    if not official:
        return NameVerdict.EIN_ABSENT, None
    for candidate in official:
        if names_agree(filed_name, candidate):
            return NameVerdict.AGREES, candidate
    best = max(
        official,
        key=lambda n: difflib.SequenceMatcher(
            None, normalize_org_name(filed_name), normalize_org_name(n)
        ).ratio(),
    )
    return NameVerdict.REVIEW, best


def load_ein_index(csv_paths: Iterable[str]) -> dict[str, set[str]]:
    """EIN -> official taxpayer name(s), from IRS annual index CSVs.

    The same file the ingest already downloads to locate filings doubles as
    the validation source, so the check costs one extra pass over a file we
    fetch anyway — no additional API, no additional trust boundary.
    """
    index: dict[str, set[str]] = defaultdict(set)
    for path in csv_paths:
        with open(path, newline="", encoding="utf-8", errors="replace") as f:
            for row in csv.DictReader(f):
                ein, name = row.get("EIN"), row.get("TAXPAYER_NAME")
                if ein and name:
                    index[ein].add(name)
    logger.info("EIN index loaded: %d distinct EINs", len(index))
    return dict(index)


# ── Filing parsing ────────────────────────────────────────────────────


@dataclass(frozen=True)
class FundingEdge:
    """One filed relationship between two organizations."""

    source_ein: str
    source_name: str
    target_ein: str | None
    target_name_as_filed: str | None
    edge_kind: str            # 'grant' | 'related_org'
    amount_usd: int | None
    purpose: str | None
    exempt_code: str | None
    fiscal_period: str        # YYYYMM
    form: str
    object_id: str
    filing_url: str
    target_name_irs: str | None = None
    verdict: NameVerdict = NameVerdict.REVIEW


def _tag(el) -> str:
    return el.tag.split("}")[-1]


def _text(el, *path: str) -> str | None:
    for step in path:
        if el is None:
            return None
        el = next((c for c in el if _tag(c) == step), None)
    return (el.text or "").strip() if el is not None and el.text else None


def _org_name(el) -> str | None:
    """Counterparty name, tolerating the schema's reuse of element names.

    Inside `IdRelatedTaxExemptOrgGrp` the IRS schema stores the related
    organization's name in a `DisregardedEntityName` element — the tag is
    reused across Schedule R parts. Reading only `RelatedOrganizationName`
    yields None for every row and looks exactly like "this filer declared
    no related orgs", which is why the fallbacks are explicit here.
    """
    for tag in ("RelatedOrganizationName", "DisregardedEntityName", "BusinessName"):
        value = _text(el, tag, "BusinessNameLine1Txt")
        if value:
            return value
    return None


def _int_or_none(value: str | None) -> int | None:
    try:
        return int(value) if value not in (None, "") else None
    except ValueError:
        return None


def parse_filing(
    xml_text: str,
    *,
    source_ein: str,
    source_name: str,
    fiscal_period: str,
    object_id: str,
    filing_url: str,
) -> list[FundingEdge]:
    """Extract Schedule I grants and Schedule R related orgs from one 990."""
    root = ET.fromstring(xml_text)
    common = dict(
        source_ein=source_ein,
        source_name=source_name,
        fiscal_period=fiscal_period,
        object_id=object_id,
        filing_url=filing_url,
    )
    edges: list[FundingEdge] = []
    for grant in (e for e in root.iter() if _tag(e) == "RecipientTable"):
        edges.append(
            FundingEdge(
                target_ein=_text(grant, "RecipientEIN"),
                target_name_as_filed=_text(
                    grant, "RecipientBusinessName", "BusinessNameLine1Txt"
                ),
                edge_kind="grant",
                amount_usd=_int_or_none(_text(grant, "CashGrantAmt")),
                purpose=_text(grant, "PurposeOfGrantTxt"),
                exempt_code=_text(grant, "IRCSectionDesc"),
                form="990 Sch I Part II",
                **common,
            )
        )
    for rel in (e for e in root.iter() if _tag(e) == "IdRelatedTaxExemptOrgGrp"):
        edges.append(
            FundingEdge(
                target_ein=_text(rel, "EIN"),
                target_name_as_filed=_org_name(rel),
                edge_kind="related_org",
                amount_usd=None,
                purpose=_text(rel, "PrimaryActivitiesTxt"),
                exempt_code=(
                    _text(rel, "ExemptCodeSectionTxt")
                    or _text(rel, "ExemptCodeSectionDesc")
                ),
                form="990 Sch R Part II",
                **common,
            )
        )
    return edges


def apply_verdicts(
    edges: Iterable[FundingEdge], ein_index: dict[str, set[str]]
) -> list[FundingEdge]:
    """Attach an `ein_name_agrees` verdict to every edge before persistence."""
    out: list[FundingEdge] = []
    for edge in edges:
        verdict, official = ein_name_agrees(
            edge.target_name_as_filed, edge.target_ein, ein_index
        )
        out.append(
            FundingEdge(
                **{
                    **edge.__dict__,
                    "verdict": verdict,
                    "target_name_irs": official,
                }
            )
        )
    counts: dict[str, int] = defaultdict(int)
    for edge in out:
        counts[edge.verdict.value] += 1
    logger.info("funding edges verdicts: %s", dict(counts))
    return out
