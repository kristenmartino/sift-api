"""Verify that every role_title_source actually states its role_title.

Guard for the Phase 4 executive dossiers. `data/executive_profiles.csv`
pairs each official with the record that establishes their office — a
U.S. Code section on uscode.house.gov, a National Archives transcript of
the constitutional provision, or an official government page for
non-statutory and foreign posts.

A citation nobody checked is how the Brookings FARA claim shipped
(`sift/STATUS.md:80-84`) and how migration 013's funder list got attached
to a 990 that legally cannot support it. So this fetches every distinct
`role_title_source` and asserts the page literally contains `role_title`.
Rows that fail are reported and MUST NOT be seeded — `seed_executive_records.py`
refuses to write a row whose source did not verify.

Read-only. No database, no API key, no cost.

    ./.venv/bin/python3 scripts/verify_role_sources.py
    ./.venv/bin/python3 scripts/verify_role_sources.py --input data/executive_profiles.csv
"""
from __future__ import annotations

import argparse
import csv
import html
import os
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import date

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_INPUT = os.path.join(REPO_ROOT, "data", "executive_profiles.csv")
DEFAULT_REPORT = os.path.join(REPO_ROOT, "data", "role_source_verification.csv")

USER_AGENT = "SiftNews/1.0 (civic dossier sourcing; +https://siftnews.kristenmartino.ai)"
_TAGS = re.compile(r"<[^>]+>")
_WS = re.compile(r"\s+")
_SECTION_HEAD = re.compile(r'<h3[^>]*class="section-head"[^>]*>', re.I)

# uscode.house.gov serves the statutory text AND every editorial/statutory note
# and cross-reference on one page — 42 U.S.C. §4321 (NEPA's declaration of
# purpose) runs 184KB and mentions "Administrator of the Environmental
# Protection Agency" inside a 2022 appropriations note. A naive substring match
# therefore "verifies" a section that does not establish the office at all.
# Everything from the first notes heading onward is cut before matching.
_NOTES_BOUNDARY = re.compile(
    r"(editorial notes|statutory notes and related subsidiaries|"
    r"historical and revision notes|amendments\b|executive documents)",
    re.I,
)


def _strip_tags(body: str) -> str:
    return _WS.sub(" ", html.unescape(_TAGS.sub(" ", body)))


def _text(url: str, *, timeout: int = 45) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
    body = raw.decode(resp.headers.get_content_charset() or "utf-8", "replace")

    if "uscode.house.gov" in url:
        # Keep only the section heading and the operative text beneath it.
        head = _SECTION_HEAD.search(body)
        if head:
            body = body[head.start():]
        text = _strip_tags(body)
        cut = _NOTES_BOUNDARY.search(text)
        if cut:
            text = text[: cut.start()]
        return text.lower()

    return _strip_tags(body).lower()


# Titles that merely *contain* the office title but name a different job.
# 5 U.S.C. §5314 lists "Deputy Administrator of the Small Business
# Administration"; a bare substring test would read that as confirming the
# Administrator. Every match is required not to be preceded by one of these.
_SUBORDINATE = r"(?:deputy|associate|assistant|under|acting|special|vice)\s+"


def _needles(role_title: str) -> list[str]:
    """Phrasings of one office title that the establishing record may use.

    Statutes are inconsistent about the connective. 10 U.S.C. §113 says
    "Secretary of Defense"; the Executive Schedule (5 U.S.C. §§5312-5315)
    lists the same offices in comma form — "Chairman, Council of Economic
    Advisers". Both name the same office, so both count.
    """
    title = _WS.sub(" ", role_title.strip().lower())
    out = {title}
    match = re.match(
        r"^(secretary|administrator|director|chair(?:man|woman|person)?|representative)"
        r"\s+of\s+(?:the\s+)?(.+)$",
        title,
    )
    if match:
        head, tail = match.group(1), match.group(2)
        out.update({
            f"{head} of the {tail}",
            f"{head} of {tail}",
            f"{head}, {tail}",          # Executive Schedule comma form
            f"{head}, the {tail}",
        })
    return sorted(out)


def _states_title(page: str, role_title: str) -> bool:
    """True when `page` names this office and not a subordinate of it."""
    for needle in _needles(role_title):
        for match in re.finditer(re.escape(needle), page):
            prefix = page[max(0, match.start() - 40): match.start()]
            if not re.search(_SUBORDINATE + r"$", prefix):
                return True
    return False


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", default=DEFAULT_INPUT)
    parser.add_argument("--report", default=DEFAULT_REPORT)
    parser.add_argument("--sleep", type=float, default=1.0)
    args = parser.parse_args()

    with open(args.input, newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))

    # One fetch per distinct URL, not per row — many officials share a statute.
    pairs: dict[str, set[str]] = {}
    # Rows carrying `verify_name` additionally require the page to name the
    # person. A U.S. statute establishes an office without naming anyone, so
    # the officeholder is evidenced separately (by a Senate roll-call). Foreign
    # rows have no such second record: the government's own page IS the whole
    # claim, so it has to carry both halves or the row does not publish.
    names: dict[str, set[str]] = {}
    for row in rows:
        src = (row.get("role_title_source") or "").strip()
        title = (row.get("role_title") or "").strip()
        if src and title:
            pairs.setdefault(src, set()).add(title)
            who = (row.get("verify_name") or "").strip()
            if who:
                names.setdefault(src, set()).add(who)

    results: dict[tuple[str, str], str] = {}
    print(f"Verifying {len(pairs)} distinct sources…", file=sys.stderr)
    for url, titles in sorted(pairs.items()):
        try:
            page = _text(url)
        except (urllib.error.URLError, urllib.error.HTTPError, OSError) as exc:
            for title in titles:
                results[(url, title)] = f"UNREACHABLE ({exc})"
            print(f"  ! {url} — {exc}", file=sys.stderr)
            time.sleep(args.sleep)
            continue
        missing_name = sorted(
            who for who in names.get(url, set()) if _WS.sub(" ", who.lower()) not in page
        )
        for title in titles:
            hit = _states_title(page, title)
            if not hit:
                verdict = "TITLE NOT FOUND"
            elif missing_name:
                verdict = f"PAGE DOES NOT NAME {missing_name[0]}"
            else:
                verdict = "OK"
            results[(url, title)] = verdict
            if verdict != "OK":
                print(f"  ! {verdict}: {title!r} @ {url}", file=sys.stderr)
        time.sleep(args.sleep)

    with open(args.report, "w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        # `verified_at` is the date the source was actually refetched, not the
        # date it is seeded. The publish floor expires foreign rows on it (see
        # migration 017), so it has to record the check, not the write — a CSV
        # seeded months after verification must not read as freshly checked.
        checked_on = date.today().isoformat()
        writer.writerow([
            "bioguide_id", "role_title", "role_title_source", "verdict", "verified_at",
        ])
        for row in rows:
            src = (row.get("role_title_source") or "").strip()
            title = (row.get("role_title") or "").strip()
            verdict = (
                results.get((src, title), "OK")
                if src and title
                else "NO SOURCE — will not be published"
            )
            writer.writerow([
                row.get("bioguide_id", ""), title, src, verdict,
                checked_on if verdict == "OK" else "",
            ])

    failed = sum(1 for v in results.values() if v != "OK")
    unsourced = sum(
        1 for r in rows
        if not (r.get("role_title_source") or "").strip()
        or not (r.get("role_title") or "").strip()
    )
    print(
        f"\n{len(rows)} rows · {len(pairs)} distinct sources · "
        f"{failed} failed · {unsourced} carry no role source (will not publish)\n"
        f"Report → {args.report}",
        file=sys.stderr,
    )
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
