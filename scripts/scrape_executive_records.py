"""Scrape the Senate + congress.gov primary records behind executive dossiers.

Phase 4 tooling. The 102 `politician_profiles` rows with
`chamber IN ('executive','foreign-executive')` shipped with a freeform
`notes` blob of uncited biographical prose about living people —
the same defect `sift/STATUS.md:103` caught in `org_profiles.notes` and
migration 013 removed. `sift/docs/OPERATING_CONTEXT.md` §5 forbids it.

This script gathers the primary records that replace that prose. It
writes a CSV; it never touches the database. `seed_executive_records.py`
does the write.

Two sources, both public records, neither of them Wikipedia:

1. **senate.gov roll-call vote menus** — `vote_menu_<congress>_<session>.xml`.
   Free, no key, no rate limit. Gives the confirmation date, the roll-call
   vote number (hence the canonical vote URL), the verbatim result and
   tally, and the statutory office name as it appears in the Senate's own
   vote title ("...to be Secretary of Defense").

2. **api.congress.gov nominations** — gives `receivedDate` (the nomination
   date) and, in `description`, the *"vice <predecessor>, resigned"* clause.
   That clause is the primary-record source for `predecessor_name`; there
   is no other machine-readable one.

Run from sift-api root:

    ./.venv/bin/python3 scripts/scrape_executive_records.py
    ./.venv/bin/python3 scripts/scrape_executive_records.py --congress 119 --congress 117

**API key.** Set `CONGRESS_API_KEY` (free, instant, emailed:
https://api.congress.gov/sign-up/). Without it the script falls back to
`DEMO_KEY`, which api.data.gov rate-limits to ~10 requests/hour — the run
then takes hours rather than a minute. Every page is cached under
`data/.congress_cache/`, so an interrupted run resumes for free and a
re-run costs zero requests.

Source 1 alone covers everything except `nomination_date` and
`predecessor_name`. `--skip-congress-api` gets you that subset with no key
and no waiting.

Scope note: this covers Senate-confirmed U.S. officials only. Presidents,
Vice Presidents, White House staff, and foreign heads of state and
government are never confirmed by the Senate and appear in neither source
— they are curated by hand in `data/executive_profiles.csv`, each with its
own primary record.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from typing import Any, Iterator

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_OUTPUT = os.path.join(REPO_ROOT, "data", "executive_confirmations.csv")
CACHE_DIR = os.path.join(REPO_ROOT, "data", ".congress_cache")

SENATE_MENU = (
    "https://www.senate.gov/legislative/LIS/roll_call_lists/"
    "vote_menu_{congress}_{session}.xml"
)
# The canonical citable URL for a single roll-call vote.
SENATE_VOTE_URL = (
    "https://www.senate.gov/legislative/LIS/roll_call_votes/"
    "vote{congress}{session}/vote_{congress}_{session}_{number:05d}.htm"
)
CONGRESS_API = "https://api.congress.gov/v3/nomination/{congress}"
CONGRESS_NOMINATION_URL = (
    "https://www.congress.gov/nomination/{congress}th-congress/{number}"
)

USER_AGENT = "SiftNews/1.0 (civic dossier sourcing; +https://siftnews.kristenmartino.ai)"
PAGE_SIZE = 250

CSV_FIELDS = [
    "congress",
    "session",
    "nominee_raw",          # verbatim from the Senate vote title
    "position_title",       # verbatim statutory office, parsed out of the title
    "nomination_citation",  # PN12, PN645-2
    "nomination_date",      # congress.gov receivedDate
    "nomination_url",       # congress.gov PN record
    "confirmation_date",
    "confirmation_vote_url",
    "confirmation_vote_result",  # "Confirmed 93-2"
    "predecessor_name",     # parsed from the congress.gov "vice ..." clause
]

# "Confirmation: Lloyd J. Austin III, of Georgia, to be Secretary of Defense"
# The colon is a modern convention — the 111th Congress (2009) writes
# "Confirmation Hillary Rodham Clinton, of New York, to be Secretary of State".
# Requiring it silently yielded zero matches for that whole Congress.
#
# The comma before "of <state>" is likewise not guaranteed: 111-2 writes
# "Confirmation Elena Kagan of Massachusetts, to be an Associate Justice of
# the Supreme Court of the U.S." Requiring it dropped 20 confirmations across
# 101-119, Kagan among them.
_TITLE_RE = re.compile(
    r"^Confirmation:?\s+(?P<name>.+?),?\s+of\s+[^,]+,\s+to\s+be\s+(?:an?\s+|the\s+)?"
    r"(?P<position>.+?)\s*$",
    re.IGNORECASE | re.DOTALL,
)
# Congresses 101-106 use a wholly different title form that names the nominee
# and nothing else: "Nomination - Clarence Thomas". Before this fallback those
# six Congresses matched zero rows and reported it as a clean run — the same
# "reports success while producing nothing" shape STATUS.md's active focus
# tracks. There is no office in the source, so `position_title` is empty and
# build_executive_profiles.py's `office_titles.get()` simply won't match these
# rows; they carry the confirmation date, vote URL and tally, which for a
# constitutionally-established office (28 U.S.C. § 1) is the whole claim.
_TITLE_LEGACY_RE = re.compile(r"^Nomination\s*[-–]\s*(?P<name>.+?)\s*$", re.IGNORECASE)
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
_MONTHS = {
    "Jan": 1, "Feb": 2, "Mar": 3, "Apr": 4, "May": 5, "Jun": 6,
    "Jul": 7, "Aug": 8, "Sep": 9, "Oct": 10, "Nov": 11, "Dec": 12,
}


# ── fetch helpers ────────────────────────────────────────────────────


def _get(url: str, *, timeout: int = 60) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def _get_json_cached(url: str, cache_key: str, *, pace: float) -> dict[str, Any]:
    """GET JSON, caching the body on disk so re-runs cost zero API calls.

    On 429 (DEMO_KEY's ~10/hour ceiling) this backs off and retries rather
    than failing the run — the whole point of the cache is that a slow run
    is survivable.
    """
    os.makedirs(CACHE_DIR, exist_ok=True)
    path = os.path.join(CACHE_DIR, cache_key)
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)

    delay = pace
    for attempt in range(12):
        try:
            body = _get(url)
            break
        except urllib.error.HTTPError as exc:
            if exc.code not in (429, 502, 503):
                raise
            wait = min(delay, 900)
            print(
                f"  … HTTP {exc.code} (rate limit); sleeping {wait:.0f}s "
                f"[attempt {attempt + 1}]",
                file=sys.stderr,
                flush=True,
            )
            time.sleep(wait)
            delay = min(delay * 1.6, 900)
    else:
        raise RuntimeError(f"gave up fetching {url}")

    data = json.loads(body)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh)
    return data


# ── source 1: senate.gov roll-call vote menus ────────────────────────


def fetch_confirmations(congress: int, session: int) -> list[dict[str, Any]]:
    """Every *confirmed* nomination roll-call in one Senate session."""
    url = SENATE_MENU.format(congress=congress, session=session)
    try:
        root = ET.fromstring(_get(url))
    except urllib.error.HTTPError as exc:
        print(f"  ! {congress}-{session}: HTTP {exc.code}", file=sys.stderr)
        return []

    year = (root.findtext("congress_year") or "").strip()
    out: list[dict[str, Any]] = []

    for vote in root.findall(".//vote"):
        question = (vote.findtext("question") or "").strip()
        result = (vote.findtext("result") or "").strip()
        if "Nomination" not in question or result != "Confirmed":
            continue

        title = " ".join((vote.findtext("title") or "").split())
        match = _TITLE_RE.match(title)
        legacy = _TITLE_LEGACY_RE.match(title) if not match else None
        if not match and not legacy:
            continue

        number = int((vote.findtext("vote_number") or "0").strip())
        yeas = (vote.findtext("vote_tally/yeas") or "").strip()
        nays = (vote.findtext("vote_tally/nays") or "").strip()

        out.append({
            "congress": congress,
            "session": session,
            "nominee_raw": (match or legacy).group("name").strip(),
            "position_title": match.group("position").strip() if match else "",
            "nomination_citation": (vote.findtext("issue") or "").strip(),
            "confirmation_date": _parse_date(
                (vote.findtext("vote_date") or "").strip(), year
            ),
            "confirmation_vote_url": SENATE_VOTE_URL.format(
                congress=congress, session=session, number=number
            ),
            "confirmation_vote_result": (
                f"Confirmed {yeas}-{nays}" if yeas and nays else result
            ),
        })

    print(f"  {congress}-{session}: {len(out)} confirmations", file=sys.stderr)
    return out


def _parse_date(raw: str, year: str) -> str:
    """'17-Dec' + '2025' → '2025-12-17'. Empty string when unparseable."""
    parts = raw.split("-")
    if len(parts) != 2 or not year:
        return ""
    day, mon = parts[0].strip(), parts[1].strip()[:3].title()
    if mon not in _MONTHS or not day.isdigit():
        return ""
    return f"{int(year):04d}-{_MONTHS[mon]:02d}-{int(day):02d}"


# ── source 2: api.congress.gov nominations ───────────────────────────


def fetch_nominations(congress: int, api_key: str, *, pace: float) -> dict[str, dict]:
    """All nominations in one Congress, keyed by PN citation.

    Paged at 250/request — ~9 requests for a Congress with ~2,000
    nominations, versus one request per nominee if fetched individually.
    """
    by_citation: dict[str, dict] = {}
    offset = 0
    while True:
        url = (
            f"{CONGRESS_API.format(congress=congress)}"
            f"?format=json&limit={PAGE_SIZE}&offset={offset}&api_key={api_key}"
        )
        data = _get_json_cached(url, f"nom_{congress}_{offset}.json", pace=pace)

        page = data.get("nominations", []) or []
        for nom in page:
            citation = (nom.get("citation") or "").strip()
            if citation:
                by_citation[citation] = nom

        total = (data.get("pagination") or {}).get("count", 0)
        offset += PAGE_SIZE
        print(
            f"  congress {congress}: {min(offset, total)}/{total} nominations",
            file=sys.stderr,
            flush=True,
        )
        if offset >= total or not page:
            break

    return by_citation


def enrich(rows: list[dict], nominations: dict[str, dict], congress: int) -> None:
    """Fill nomination_date / nomination_url / predecessor_name in place."""
    for row in rows:
        if row["congress"] != congress:
            continue
        nom = nominations.get(row["nomination_citation"])
        if not nom:
            continue
        row["nomination_date"] = nom.get("receivedDate") or ""
        row["nomination_url"] = CONGRESS_NOMINATION_URL.format(
            congress=congress, number=nom.get("number", "")
        )
        match = _VICE_RE.search(nom.get("description") or "")
        if match:
            row["predecessor_name"] = " ".join(match.group("pred").split())


# ── main ─────────────────────────────────────────────────────────────


def _sessions(congress: int) -> Iterator[tuple[int, int]]:
    yield congress, 1
    yield congress, 2


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--congress",
        type=int,
        action="append",
        help="Congress to read Senate roll-calls for; repeatable. Default: "
             "111 through 119. senate.gov is free and unmetered, so this range "
             "is deliberately CONTIGUOUS — build_executive_profiles.py infers a "
             "former official's end date from the successor's confirmation, and "
             "refuses to infer across a Congress that was never read. A sparse "
             "range would silently withhold end dates (or, worse, skip a real "
             "successor) for every office whose handover fell in the hole.",
    )
    parser.add_argument(
        "--enrich-congress",
        type=int,
        action="append",
        help="Congress to also pull nomination_date + predecessor from "
             "api.congress.gov for; repeatable. Default: 111, 117, 119 — the "
             "ones the executive dossiers actually cite. Kept narrower than "
             "--congress because this is the rate-limited half: each Congress "
             "is ~9-12 paged requests, and DEMO_KEY allows ~10 per hour.",
    )
    parser.add_argument("--output", default=DEFAULT_OUTPUT)
    parser.add_argument(
        "--skip-congress-api",
        action="store_true",
        help="Senate roll-calls only. No key needed, no rate limit, but no "
             "nomination_date and no predecessor_name.",
    )
    parser.add_argument(
        "--pace",
        type=float,
        default=60.0,
        help="Initial back-off seconds after a 429. DEMO_KEY needs ~360.",
    )
    args = parser.parse_args()

    congresses = args.congress or list(range(111, 120))
    enrich_congresses = args.enrich_congress or [111, 117, 119]
    api_key = os.environ.get("CONGRESS_API_KEY", "").strip() or "DEMO_KEY"
    if api_key == "DEMO_KEY" and not args.skip_congress_api:
        print(
            "! CONGRESS_API_KEY unset — falling back to DEMO_KEY (~10 req/hour).\n"
            "  Pages are cached under data/.congress_cache/, so this is resumable.\n"
            "  Get a free key at https://api.congress.gov/sign-up/ to run it in "
            "under a minute.",
            file=sys.stderr,
        )

    rows: list[dict[str, Any]] = []
    print("Senate roll-call vote menus:", file=sys.stderr)
    for congress in congresses:
        for cong, session in _sessions(congress):
            rows.extend(fetch_confirmations(cong, session))

    for row in rows:
        row.setdefault("nomination_date", "")
        row.setdefault("nomination_url", "")
        row.setdefault("predecessor_name", "")

    if not args.skip_congress_api:
        print("congress.gov nominations:", file=sys.stderr)
        for congress in enrich_congresses:
            noms = fetch_nominations(congress, api_key, pace=args.pace)
            enrich(rows, noms, congress)

    rows.sort(key=lambda r: (r["congress"], r["session"], r["confirmation_date"]))

    os.makedirs(os.path.dirname(args.output), exist_ok=True)
    with open(args.output, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=CSV_FIELDS)
        writer.writeheader()
        for row in rows:
            writer.writerow({k: row.get(k, "") for k in CSV_FIELDS})

    with_pred = sum(1 for r in rows if r["predecessor_name"])
    print(
        f"\nWrote {len(rows)} confirmations → {args.output}\n"
        f"  nomination_date:  {sum(1 for r in rows if r['nomination_date'])}\n"
        f"  predecessor_name: {with_pred}",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
