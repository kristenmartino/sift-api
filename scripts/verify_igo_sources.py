"""Verify that each IGO's governance claim is actually in its founding treaty.

Sibling to `verify_role_sources.py`, for `data/igo_profiles.csv`.

Q8 closed "global" (see `sift/docs/DECISIONS.md` D47). Intergovernmental
organizations are the entry point rather than foreign heads of state,
and the reason is sourcing: a head of government is evidenced only by a
page that names them today, which decays and which 33 of 46 times could
not be fetched at all. A treaty is a fixed document at a stable URL. It
does not decay, so these rows need no `role_verified_at` equivalent.

`governance_structure` is a paraphrase, matching the register the 93
agency rows already use ("Independent regulatory commission. Six voting
members appointed by the President..."). A paraphrase cannot be checked
by substring, so each row also carries `verify_phrases`: pipe-separated
strings quoted from the treaty that the paraphrase rests on. **Every one
must appear on the cited page or the row is refused.**

That is deliberately stricter than `verify_role_sources.py`, whose
weakness is on the record: `iletisim.gov.tr` passed it because two
strings happened to be present, on a page that was a syndicated
press-clipping feed. Requiring several specific phrases from the
document being cited is much harder to satisfy by accident — but it is
still a filter, not sign-off. Read the page.

Read-only. No database, no API key, no cost.

    ./.venv/bin/python3 scripts/verify_igo_sources.py
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
DEFAULT_INPUT = os.path.join(REPO_ROOT, "data", "igo_profiles.csv")
DEFAULT_REPORT = os.path.join(REPO_ROOT, "data", "igo_source_verification.csv")

USER_AGENT = "SiftNews/1.0 (civic dossier sourcing; +https://siftnews.kristenmartino.ai)"
_TAGS = re.compile(r"<(script|style)[^>]*>.*?</\1>|<[^>]+>", re.S | re.I)
_WS = re.compile(r"\s+")


def _text(url: str, *, timeout: int = 60) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
    body = raw.decode(resp.headers.get_content_charset() or "utf-8", "replace")
    return _WS.sub(" ", html.unescape(_TAGS.sub(" ", body))).lower()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", default=DEFAULT_INPUT)
    parser.add_argument("--report", default=DEFAULT_REPORT)
    parser.add_argument("--sleep", type=float, default=1.0)
    args = parser.parse_args()

    with open(args.input, newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))

    checked_on = date.today().isoformat()
    results: list[tuple[str, str, str]] = []
    failures = 0

    print(f"Verifying {len(rows)} IGO sources…", file=sys.stderr)
    for row in rows:
        slug = row["slug"].strip()
        source = (row.get("governance_source") or "").strip()
        phrases = [p.strip() for p in (row.get("verify_phrases") or "").split("|") if p.strip()]

        if not source or not phrases:
            results.append((slug, source, "NO SOURCE OR NO PHRASES — will not be seeded"))
            failures += 1
            continue

        try:
            page = _text(source)
        except (urllib.error.URLError, urllib.error.HTTPError, OSError) as exc:
            results.append((slug, source, f"UNREACHABLE ({exc})"))
            failures += 1
            print(f"  ! {slug}: {exc}", file=sys.stderr)
            time.sleep(args.sleep)
            continue

        missing = [p for p in phrases if p.lower() not in page]
        if missing:
            results.append((slug, source, f"MISSING: {'; '.join(missing)}"))
            failures += 1
            print(f"  ! {slug}: not on the page — {'; '.join(missing)}", file=sys.stderr)
        else:
            results.append((slug, source, "OK"))
            print(f"  OK   {slug} ({len(phrases)} phrases)", file=sys.stderr)
        time.sleep(args.sleep)

    with open(args.report, "w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(["slug", "governance_source", "verdict", "verified_at"])
        for slug, source, verdict in results:
            writer.writerow([slug, source, verdict, checked_on if verdict == "OK" else ""])

    print(
        f"\n{len(rows)} rows · {failures} failed\nReport → {args.report}",
        file=sys.stderr,
    )
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
