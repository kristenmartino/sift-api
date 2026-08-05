"""Select the sitting Justices' confirmation records out of the Senate scrape.

Phase 4 tooling, sibling to `build_executive_profiles.py`.

`scrape_executive_records.py` reads every *confirmed* nomination roll-call
from the senate.gov vote menus. This script picks out the ones belonging to
sitting Supreme Court Justices and writes `data/scotus_confirmations.csv`,
which `seed_scotus_records.py` then applies to `politician_profiles`.

Why the Justices need their own selector rather than the executive path:

1. **The office is not in the vote title for all of them.** Congresses
   101-106 title a confirmation "Nomination - Clarence Thomas" and stop —
   no state, no office. `build_executive_profiles.py` keys on the office
   string, so it cannot see those rows at all.

2. **The office does not need to be in the vote title.** Every Justice
   holds one of exactly two offices, both created by a single statute
   (28 U.S.C. § 1: "The Supreme Court of the United States shall consist
   of a Chief Justice of the United States and eight associate justices").
   So `role_title` / `role_title_source` are the same two values for all
   nine rows, and neither is recalled — they are read off the statute.

What each row's claim rests on:

    role_title               <- 28 U.S.C. § 1 on govinfo.gov (GPO)
    confirmation_date        <- senate.gov roll-call vote menu
    confirmation_vote_result <- same, verbatim tally
    confirmation_vote_url    <- same, the roll-call page itself

**Verification is not optional here.** A roll-call URL is constructed from
a vote number, and senate.gov returns HTTP 200 for vote numbers that exist
but belong to some other vote entirely — so a 200 proves nothing. With
`--verify` (default on) every constructed URL is fetched and the nominee's
surname must appear on the page, otherwise the row is refused. This is the
known-true-case check `STATUS.md`'s active focus asks for: the roster is
nine names known in advance, so a selector that returns eight or ten is
wrong by construction and says so.

Run from sift-api root, after the scrape:

    ./.venv/bin/python3 scripts/scrape_executive_records.py \\
        --congress 101 ... --congress 119 --skip-congress-api \\
        --output data/all_confirmations.csv
    ./.venv/bin/python3 scripts/build_scotus_records.py \\
        --confirmations data/all_confirmations.csv
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
import urllib.error
import urllib.request

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_OUTPUT = os.path.join(REPO_ROOT, "data", "scotus_confirmations.csv")

USER_AGENT = "SiftNews/1.0 (civic dossier sourcing; +https://siftnews.kristenmartino.ai)"

# 28 U.S.C. § 1 — "Number of justices; quorum". GPO's govinfo copy, which
# serves the statute text as static HTML. uscode.house.gov's granuleid URL
# answers 200 with a 4 KB stub that contains none of the section text, so it
# is not citable as a source a reader can check.
USC_28_1 = (
    "https://www.govinfo.gov/content/pkg/USCODE-2023-title28/html/"
    "USCODE-2023-title28-partI-chap1-sec1.htm"
)
# Both titles are the statute's own words. § 1 reads: "The Supreme Court of the
# United States shall consist of a Chief Justice of the United States and eight
# associate justices". So the Chief's title is verbatim, and the associates' is
# the singular of the phrase the statute uses — NOT the longer "Associate
# Justice of the Supreme Court of the United States" that the Senate's recent
# vote titles use, which § 1 does not contain and which verify_role_sources.py
# therefore (correctly) refuses.
CHIEF_TITLE = "Chief Justice of the United States"
ASSOCIATE_TITLE = "Associate Justice"

# The nine sitting Justices, by the surname the Senate vote title uses and the
# canonical_id already in use across articles.entity_links. This roster is the
# *input* to the selection, not data about the people — every claim written to
# the DB comes from the records above. Ordered by seniority of commission.
ROSTER: list[tuple[str, str, bool]] = [
    # (canonical_id, surname as it appears in the vote title, is_chief)
    ("SCOTUS-ROBERTS-J", "Roberts", True),
    ("SCOTUS-THOMAS-C", "Thomas", False),
    ("SCOTUS-ALITO-S", "Alito", False),
    ("SCOTUS-SOTOMAYOR-S", "Sotomayor", False),
    ("SCOTUS-KAGAN-E", "Kagan", False),
    ("SCOTUS-GORSUCH-N", "Gorsuch", False),
    ("SCOTUS-KAVANAUGH-B", "Kavanaugh", False),
    ("SCOTUS-BARRETT-A", "Barrett", False),
    ("SCOTUS-JACKSON-KB", "Jackson", False),
]

CSV_FIELDS = [
    # The Senate's verbatim nominee string ("John G. Roberts, Jr."). Kept as
    # provenance for the match, NOT written to politician_profiles.name —
    # that column is the linker's matching surface and articles say "John
    # Roberts", the same common-name form the EXEC-* rows use.
    "bioguide_id",
    "nominee_raw",
    "role_title",
    "role_title_source",
    "confirmation_date",
    "confirmation_vote_url",
    "confirmation_vote_result",
    "nomination_citation",
    "nomination_date",
    "nomination_url",
    "predecessor_name",
    "predecessor_source",
]

CONGRESS_NOMINATION_API = "https://api.congress.gov/v3/nomination/{congress}/{number}"
CONGRESS_NOMINATION_URL = (
    "https://www.congress.gov/nomination/{congress}th-congress/{number}"
)
# "...to be Chief Justice of the United States, vice William H. Rehnquist,
# deceased." The name is everything between "vice" and the disposition word.
#
# Two things this must get right, both of which an earlier version got wrong:
#   - Periods belong INSIDE the name. Excluding them truncated "William H.
#     Rehnquist" to "William H" and "Stephen G. Breyer" to "Stephen G".
#   - "retiring" is a disposition. Omitting it dropped the clause entirely for
#     Alito and Kagan while silently truncating four others -- a partial result
#     that looked like data.
# `.+?` is non-greedy, so a suffixed name ("vice John Smith, Jr., retired")
# still extends past the first comma to reach the real disposition.
_VICE_RE = re.compile(
    r",\s*vice\s+(?P<pred>.+?)"
    r"(?:,\s*(?:resigned|retired|retiring|deceased|elevated|removed|"
    r"term\s+expir\w*)\b|\.\s*$)",
    re.IGNORECASE,
)

# A Justice's confirmation is a roll-call whose parsed office mentions a
# Justice seat, OR — for the pre-107th title form — one that carries no office
# at all. The second case is why an exact-office filter cannot be used.
_JUSTICE_POS = re.compile(r"\bjustice\b", re.IGNORECASE)

# Generational suffixes sit after the surname and must be stripped before the
# last token means anything: "John G. Roberts, Jr." -> "Roberts".
_SUFFIXES = {"jr", "sr", "ii", "iii", "iv", "v"}


def surname_of(nominee_raw: str) -> str:
    """Last name-bearing token of a Senate vote title's nominee string.

    A substring test is not sufficient: matching 'Thomas' anywhere pulls in
    'Thomas W. Payzant' and 'thomas s. foley to be ambassador to japan',
    both of which the pre-107th title form leaves without an office to
    filter on. The surname is the last token that is not a suffix.
    """
    tokens = [t.strip(" ,.") for t in nominee_raw.split()]
    while tokens and tokens[-1].lower().strip(".") in _SUFFIXES:
        tokens.pop()
    return tokens[-1] if tokens else ""


def _fetch(url: str, *, timeout: int = 60) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", errors="replace")


def candidates(rows: list[dict[str, str]], surname: str) -> list[dict[str, str]]:
    """Confirmation roll-calls that could be this Justice's."""
    out = []
    for row in rows:
        if surname_of(row["nominee_raw"]).lower() != surname.lower():
            continue
        position = row.get("position_title", "")
        if position and not _JUSTICE_POS.search(position):
            continue  # a different official who shares the surname
        out.append(row)
    return out


def verify(url: str, surname: str) -> bool:
    """A constructed roll-call URL must actually be this nominee's vote.

    senate.gov answers 200 for any vote number that exists, so the status
    code proves nothing on its own.
    """
    try:
        page = _fetch(url)
    except (urllib.error.URLError, urllib.error.HTTPError) as exc:
        print(f"    ! fetch failed: {exc}", file=sys.stderr)
        return False
    return surname.lower() in page.lower()


def enrich_nomination(congress: int, number: str, api_key: str) -> dict[str, str]:
    """`receivedDate` and the "vice <name>" clause for one PN record.

    A Justice's seat is always filled *vice* a named predecessor — unlike the
    executive rows, where an incoming administration files its Cabinet en bloc
    and the clause is absent on 35 of 37. So for the Court this is the primary
    record for `predecessor_name`, not a fallback, and it says what the prior
    holder did: "vice Ruth Bader Ginsburg, deceased".

    Returns {} on any failure. A missing PN record leaves the columns NULL,
    which simply does not render (013's pattern); it must never leave a value
    behind without its source.
    """
    url = CONGRESS_NOMINATION_API.format(congress=congress, number=number)
    req = urllib.request.Request(
        f"{url}?format=json&api_key={api_key}",
        # api.data.gov answers 403 to urllib's default User-Agent.
        headers={"User-Agent": USER_AGENT},
    )
    try:
        with urllib.request.urlopen(req, timeout=45) as resp:
            nom = json.load(resp).get("nomination") or {}
    except (urllib.error.URLError, urllib.error.HTTPError, OSError, ValueError):
        return {}

    received = (nom.get("receivedDate") or "").strip()
    if not received:
        return {}
    public_url = CONGRESS_NOMINATION_URL.format(congress=congress, number=number)
    out = {"nomination_date": received, "nomination_url": public_url}
    match = _VICE_RE.search(nom.get("description") or "")
    if match:
        out["predecessor_name"] = " ".join(match.group("pred").split())
        out["predecessor_source"] = public_url
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--confirmations",
        required=True,
        help="CSV written by scrape_executive_records.py (needs Congresses "
             "101-119; Thomas's 1991 confirmation is in the 102nd).",
    )
    parser.add_argument("--output", default=DEFAULT_OUTPUT)
    parser.add_argument(
        "--no-verify",
        action="store_true",
        help="Skip fetching each roll-call page to confirm it is the right "
             "vote. Only for offline re-runs; the check is the point.",
    )
    args = parser.parse_args()

    api_key = os.environ.get("CONGRESS_API_KEY", "").strip()
    if not api_key:
        print(
            "! CONGRESS_API_KEY unset — nomination_date and predecessor_name "
            "will be left NULL. Free key: https://api.congress.gov/sign-up/",
            file=sys.stderr,
        )

    with open(args.confirmations, newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    print(f"Read {len(rows)} confirmations from {args.confirmations}\n", file=sys.stderr)

    out: list[dict[str, str]] = []
    failures: list[str] = []

    for canonical_id, surname, is_chief in ROSTER:
        hits = candidates(rows, surname)
        if len(hits) != 1:
            failures.append(
                f"{canonical_id}: expected exactly 1 matching roll-call for "
                f"'{surname}', found {len(hits)}"
                + (
                    " — " + "; ".join(
                        f"{h['confirmation_date']} {h['nominee_raw']!r}"
                        f" pos={h['position_title']!r}"
                        for h in hits
                    )
                    if hits else ""
                )
            )
            continue

        hit = hits[0]
        url = hit["confirmation_vote_url"]
        if not args.no_verify:
            ok = verify(url, surname)
            print(
                f"  {'OK  ' if ok else 'FAIL'} {canonical_id:20} {url}",
                file=sys.stderr,
            )
            if not ok:
                failures.append(
                    f"{canonical_id}: roll-call page does not mention "
                    f"'{surname}' — {url}"
                )
                continue

        out.append({
            "bioguide_id": canonical_id,
            "nominee_raw": hit["nominee_raw"],
            "role_title": CHIEF_TITLE if is_chief else ASSOCIATE_TITLE,
            "role_title_source": USC_28_1,
            "confirmation_date": hit["confirmation_date"],
            "confirmation_vote_url": url,
            "confirmation_vote_result": hit["confirmation_vote_result"],
            "nomination_citation": hit.get("nomination_citation", ""),
            "nomination_date": "",
            "nomination_url": "",
            "predecessor_name": "",
            "predecessor_source": "",
        })

        citation = hit.get("nomination_citation", "").lstrip("PN").split("-")[0]
        if api_key and citation.isdigit():
            out[-1].update(enrich_nomination(
                int(hit["congress"]), citation, api_key
            ))

    if failures:
        print("\nRefused to write — unresolved rows:", file=sys.stderr)
        for line in failures:
            print(f"  ! {line}", file=sys.stderr)

    if len(out) != len(ROSTER):
        print(
            f"\n{len(out)}/{len(ROSTER)} Justices resolved. The roster is known "
            f"in advance, so a partial result is a failure, not a result.",
            file=sys.stderr,
        )
        return 1

    os.makedirs(os.path.dirname(args.output), exist_ok=True)
    with open(args.output, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=CSV_FIELDS)
        writer.writeheader()
        writer.writerows(out)

    print(f"\nWrote {len(out)}/{len(ROSTER)} → {args.output}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
