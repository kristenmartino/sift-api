"""Scrape the LCV National Environmental Scorecard into data/lcv_scores.csv.

Run from sift-api root:
    ./.venv/bin/python3 scripts/scrape_lcv_scorecard.py
    ./.venv/bin/python3 scripts/scrape_lcv_scorecard.py --out data/lcv_scores.csv

What this is, and what it is not
--------------------------------
The League of Conservation Voters scores members of Congress on votes it
selects as environmental. It is **one advocacy group's scorecard**, not a
neutral measure, and Sift renders it that way: a labelled line attributing a
named third party's own number, with a link to their page for that member —
the same treatment `outlet_profiles` gives AllSides and MBFC.

Every row therefore carries its own `source_url` (LCV's per-member page) and
`year`. A score without those two is dropped rather than stored, because an
uncited number about a living person is the defect migrations 013 and 015
each had to remove.

**Why LCV alone.** A symmetric conservative counterpart was attempted first
and is not obtainable: ACU/CPAC publishes 15 of ~540 lawmakers per page
behind a Wix collection with no pagination in the DOM, no href on the cards,
and data fetched through a client worker (so no endpoint to call); its legacy
system 403s. Heritage Action serves a JS shell, ADA 404s, Vote Smart 403s.
Verified 2026-08-07 with plain HTTP and with a real browser. Until a
conservative scorecard is obtainable, the UI must not call this
"interest-group ratings" in the plural — it is one lens and says so.

No DB writes. Output is a CSV for `seed_lcv_scores.py` to consume, matching
the scrape/seed split the politician pipeline already uses.
"""
from __future__ import annotations

import argparse
import csv
import os
import re
import sys

# Add parent dir to path so imports resolve
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import httpx

SCORECARD_URL = "https://www.lcv.org/congressional-scorecard/members-of-congress/"


_UA = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    )
}

# One <li class="congress-item"> per member. Non-greedy to the closing </li>.
_ITEM_RE = re.compile(r'<li class="congress-item">(.*?)</li>', re.S)
_STATE_RE = re.compile(r'<h5 class="state-title">\s*([^<]+?)\s*</h5>', re.S)
_LINK_RE = re.compile(r'<a href="([^"]+)" class="card-link">\s*([^<]+?)\s*</a>', re.S)
_PARTY_RE = re.compile(r'<p class="congress-party [^"]*">\s*([A-Z])\s*</p>', re.S)
_DISTRICT_RE = re.compile(r'congress-district')
# The two score blocks are NOT symmetric in the markup, which is easy to miss:
# the annual one labels itself with a nested <span class="year">, while only the
# lifetime one uses <span class="label">. A single regex over both silently
# matches lifetime only, leaves `annual` None, and skips every member.
_ANNUAL_RE = re.compile(
    r'<p class="congress-year-score">\s*'
    r'<span class="data-score">\s*([0-9]{1,3})%\s*</span>\s*'
    r'<span class="year-score">\s*<span class="year">\s*(20\d{2})\s*</span>',
    re.S,
)
_LIFETIME_RE = re.compile(
    r'<p class="congress-lifetime-score">\s*'
    r'<span class="data-score">\s*([0-9]{1,3})%\s*</span>',
    re.S,
)


def fetch_html(url: str = SCORECARD_URL) -> str:
    with httpx.Client(headers=_UA, follow_redirects=True, timeout=60) as c:
        r = c.get(url)
        r.raise_for_status()
        return r.text


def parse(html: str) -> list[dict]:
    """Extract one row per scored member.

    State comes from the enclosing `.state-listing` heading, so items are
    walked in document order and the most recent heading wins. Chamber is
    inferred from `congress-district`, which LCV emits only for House members
    — there is no explicit chamber attribute anywhere in the markup.
    """
    rows: list[dict] = []
    # Index every state heading so each item can be attributed to the last one
    # that preceded it.
    headings = [(m.start(), m.group(1).strip()) for m in _STATE_RE.finditer(html)]

    for item in _ITEM_RE.finditer(html):
        block = item.group(1)
        link = _LINK_RE.search(block)
        if not link:
            continue
        source_url, display_name = link.group(1).strip(), link.group(2).strip()

        state = ""
        for pos, name in headings:
            if pos < item.start():
                state = name
            else:
                break

        annual_m = _ANNUAL_RE.search(block)
        # A member with no annual score (sworn in mid-cycle) is skipped: a
        # lifetime figure alone would render as "their score" and mislead.
        if not annual_m:
            continue
        lifetime_m = _LIFETIME_RE.search(block)

        party_m = _PARTY_RE.search(block)
        rows.append({
            "lcv_name": display_name,
            "state": state,
            "party": party_m.group(1) if party_m else "",
            "chamber": "house" if _DISTRICT_RE.search(block) else "senate",
            # Year read from the markup, never assumed — the label carries it.
            "year": int(annual_m.group(2)),
            "score": int(annual_m.group(1)),
            "lifetime_score": int(lifetime_m.group(1)) if lifetime_m else "",
            "source_url": source_url,
        })
    return rows


def main() -> int:
    ap = argparse.ArgumentParser(description="Scrape the LCV scorecard to CSV.")
    ap.add_argument("--out", default="data/lcv_scores.csv")
    ap.add_argument("--url", default=SCORECARD_URL)
    args = ap.parse_args()

    html = fetch_html(args.url)
    rows = parse(html)
    if not rows:
        print("ERROR: parsed 0 rows — the page structure probably changed.",
              file=sys.stderr)
        return 1

    by_chamber: dict[str, int] = {}
    for r in rows:
        by_chamber[r["chamber"]] = by_chamber.get(r["chamber"], 0) + 1

    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    fields = ["lcv_name", "state", "party", "chamber", "year", "score",
              "lifetime_score", "source_url"]
    with open(args.out, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)

    years = sorted({r["year"] for r in rows})
    print(f"Parsed {len(rows)} scored members — {by_chamber}")
    print(f"Scorecard year(s): {years}")
    print(f"Wrote {args.out}")
    print()
    print("Next: railway run ./.venv/bin/python3 scripts/seed_lcv_scores.py --dry-run")
    return 0


if __name__ == "__main__":
    sys.exit(main())
