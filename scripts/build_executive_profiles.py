"""Assemble data/executive_profiles.csv — the reviewable Phase 4 artifact.

Joins three inputs into the one file a human reviews and
`seed_executive_records.py` writes:

  data/executive_offices.csv      office_key -> role_title, role_title_source,
                                  official_url. One row per office, not per
                                  person, so a statutory citation is authored
                                  and checked exactly once.
  data/executive_assignments.csv  bioguide_id -> office_key, and either a
                                  nomination citation (Senate-confirmed) or
                                  explicit role dates + their source (elected
                                  and appointed posts, which no roll-call
                                  covers).
  data/executive_confirmations.csv  scraped primary records, keyed by PN
                                  citation. Written by
                                  scrape_executive_records.py.

Nothing here is authored from recall. The only hand-entered values are the
join keys (which office, which PN) and the archives.gov term dates for
elected offices — every claim rendered on the page traces to a URL in the
output, and `verify_role_sources.py` refetches each one.

Also derives `role_end_date` for Senate-confirmed officials mechanically:
if a *later* confirmation to the same office exists in the scraped set, the
office passed to a Senate-confirmed successor on that date. That is a
primary-record fact with a citable roll-call, unlike "left office in
January 2025", which no record states. Rows whose office has had no
successor confirmed are left open-ended — they are the incumbents.

Run from sift-api root:
    ./.venv/bin/python3 scripts/build_executive_profiles.py
"""
from __future__ import annotations

import argparse
import csv
import os
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(REPO_ROOT, "data")

OFFICES = os.path.join(DATA, "executive_offices.csv")
ASSIGNMENTS = os.path.join(DATA, "executive_assignments.csv")
CONFIRMATIONS = os.path.join(DATA, "executive_confirmations.csv")
DEFAULT_OUTPUT = os.path.join(DATA, "executive_profiles.csv")

CSV_FIELDS = [
    "bioguide_id",
    "id_source",
    "role_title",
    "role_title_source",
    "role_start_date",
    "role_end_date",
    "role_dates_source",
    "nomination_date",
    "nomination_url",
    "confirmation_date",
    "confirmation_vote_url",
    "confirmation_vote_result",
    "predecessor_name",
    "predecessor_source",
    "official_url",
    "verify_name",
]


def _read(path: str) -> list[dict[str, str]]:
    if not os.path.exists(path):
        print(f"! missing {path}", file=sys.stderr)
        return []
    with open(path, newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", default=DEFAULT_OUTPUT)
    args = parser.parse_args()

    offices = {r["office_key"]: r for r in _read(OFFICES)}
    assignments = _read(ASSIGNMENTS)
    confirmations = _read(CONFIRMATIONS)
    by_pn = {r["nomination_citation"]: r for r in confirmations if r.get("nomination_citation")}

    # office_key -> [(confirmation_date, roll-call url, congress, nominee)],
    # ascending. Used to find the successor whose confirmation ended a former
    # official's tenure, and the predecessor whose confirmation preceded theirs.
    office_titles = {r["role_title"]: k for k, r in offices.items()}
    timeline: dict[str, list[tuple[str, str, int, str]]] = {}
    scraped_congresses: set[int] = set()
    for row in confirmations:
        try:
            congress = int(row.get("congress") or 0)
        except ValueError:
            continue
        scraped_congresses.add(congress)
        key = office_titles.get((row.get("position_title") or "").strip())
        if key and row.get("confirmation_date"):
            timeline.setdefault(key, []).append((
                row["confirmation_date"],
                row.get("confirmation_vote_url", ""),
                congress,
                row.get("nominee_raw", ""),
            ))
    for entries in timeline.values():
        entries.sort()

    def predecessor(office_key: str, before_date: str, to_congress: int):
        """The Senate's previous confirmation to this office.

        The nomination record's "vice <name>" clause is the better source and
        is preferred wherever it exists — but an incoming administration files
        its Cabinet en bloc and those entries carry no clause at all (PN11-1 is
        just "Scott Bessent, of South Carolina, to be Secretary of the
        Treasury"), because the office falls vacant at the transition rather
        than "vice" a named person. That leaves 2 of 37 rows covered.

        This fills the rest with a narrower, still-primary claim: whom the
        Senate last confirmed to the office. It is silent about acting
        officials, who are never confirmed — so the UI must not render it as a
        flat "preceded by". Same contiguity guard as `successor`: an earlier
        holder confirmed in a Congress we never read is invisible here, and
        inventing one would be worse than omitting it.
        """
        earlier = [e for e in timeline.get(office_key, []) if e[0] < before_date]
        if not earlier:
            return None
        date, url, congress, nominee = earlier[-1]
        gap = [
            c for c in range(congress + 1, to_congress)
            if c not in scraped_congresses
        ]
        if gap:
            return None
        return nominee, url

    def successor(office_key: str, after_date: str, from_congress: int):
        """The next confirmation to this office — only if nothing was missed.

        The scraped set is not contiguous: it covers the Congresses the
        executive rows actually span, not every Congress. Without this guard
        the "next" confirmation to Secretary of State after Clinton (111th,
        2009) is Blinken (117th, 2021), because Kerry's 2013 confirmation was
        never fetched — which would publish the claim that Clinton held the
        office until 2021. Refuse to infer across a Congress we did not read.
        """
        later = [e for e in timeline.get(office_key, []) if e[0] > after_date]
        if not later:
            return None
        date, url, congress, _nominee = later[0]
        gap = [
            c for c in range(from_congress + 1, congress)
            if c not in scraped_congresses
        ]
        if gap:
            return None
        return date, url

    out: list[dict[str, str]] = []
    problems: list[str] = []

    for row in assignments:
        bioguide = (row.get("bioguide_id") or "").strip()
        office_key = (row.get("office_key") or "").strip()
        if not bioguide:
            continue
        # Foreign rows carry their own source: there is no shared statute
        # behind "Prime Minister of Australia", and the only record is that
        # government's own page, which is person-specific. So an assignment may
        # supply role_title / role_title_source / official_url inline instead
        # of pointing at an office row.
        office = offices.get(office_key)
        if not office:
            if row.get("role_title") and row.get("role_title_source"):
                office = {
                    "role_title": row["role_title"],
                    "role_title_source": row["role_title_source"],
                    "official_url": row.get("official_url", ""),
                }
            else:
                problems.append(f"{bioguide}: unknown office_key {office_key!r}")
                continue

        rec = {
            "bioguide_id": bioguide,
            "id_source": (row.get("id_source") or "").strip() or "executive",
            "verify_name": (row.get("verify_name") or "").strip(),
            "role_title": office["role_title"],
            "role_title_source": office["role_title_source"],
            "role_start_date": (row.get("role_start_date") or "").strip(),
            "role_end_date": (row.get("role_end_date") or "").strip(),
            "role_dates_source": (row.get("role_dates_source") or "").strip(),
            "nomination_date": "",
            "nomination_url": "",
            "confirmation_date": "",
            "confirmation_vote_url": "",
            "confirmation_vote_result": "",
            "predecessor_name": "",
            "predecessor_source": "",
            "official_url": office.get("official_url", ""),
        }

        pn = (row.get("nomination_citation") or "").strip()
        if pn:
            conf = by_pn.get(pn)
            if not conf:
                problems.append(f"{bioguide}: no scraped confirmation for {pn}")
            else:
                rec["nomination_date"] = conf.get("nomination_date", "")
                rec["nomination_url"] = conf.get("nomination_url", "")
                rec["confirmation_date"] = conf.get("confirmation_date", "")
                rec["confirmation_vote_url"] = conf.get("confirmation_vote_url", "")
                rec["confirmation_vote_result"] = conf.get("confirmation_vote_result", "")
                # Prefer the nomination's verbatim "vice <name>" clause.
                if conf.get("predecessor_name"):
                    rec["predecessor_name"] = conf["predecessor_name"]
                    rec["predecessor_source"] = rec["nomination_url"]
                else:
                    found = predecessor(
                        office_key,
                        rec["confirmation_date"],
                        int(conf.get("congress") or 0),
                    )
                    if found:
                        rec["predecessor_name"], rec["predecessor_source"] = found

                # Successor = the next confirmation to the same office.
                # role_start_date stays blank on purpose: the roll-call dates
                # the confirmation, not the swearing-in, and confirmation_date
                # already carries that fact with its own citation.
                if not rec["role_end_date"]:
                    found = successor(
                        office_key,
                        rec["confirmation_date"],
                        int(conf.get("congress") or 0),
                    )
                    if found:
                        rec["role_end_date"], rec["role_dates_source"] = found
                    elif timeline.get(office_key) and any(
                        e[0] > rec["confirmation_date"]
                        for e in timeline[office_key]
                    ):
                        # Someone else has since held this office, but the
                        # handover date is not in the scraped record. Rendering
                        # the title with no end date would assert they still
                        # hold it. Withhold the title entirely — the row keeps
                        # its cleared notes and simply does not publish.
                        problems.append(
                            f"{bioguide}: succeeded in this office but the "
                            f"handover falls in an unscraped Congress — "
                            f"role_title withheld, will not publish"
                        )
                        rec["role_title"] = ""
                        rec["role_title_source"] = ""

        if not rec["role_title_source"]:
            problems.append(f"{bioguide}: role_title has no source — will not publish")
        out.append(rec)

    out.sort(key=lambda r: r["bioguide_id"])
    with open(args.output, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=CSV_FIELDS)
        writer.writeheader()
        writer.writerows(out)

    confirmed = sum(1 for r in out if r["confirmation_vote_url"])
    with_pred = sum(1 for r in out if r["predecessor_name"])
    ended = sum(1 for r in out if r["role_end_date"])
    print(
        f"Wrote {len(out)} rows → {args.output}\n"
        f"  with a Senate roll-call:      {confirmed}\n"
        f"  with a sourced predecessor:   {with_pred}\n"
        f"  with a sourced end date:      {ended}",
        file=sys.stderr,
    )
    for problem in problems:
        print(f"  ! {problem}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
