"""Ingest 990 funding edges for a set of organizations.

    python scripts/ingest_funding_edges.py --year 2025 --dry-run
    python scripts/ingest_funding_edges.py --year 2025 --apply

Reads Schedule I (grants paid) and Schedule R (related tax-exempt orgs) from
the IRS e-file corpus for the EINs in ORGS, checks every counterparty EIN
against the IRS's own name for it, and writes rows to `funding_edges`.

Why it is shaped this way
─────────────────────────
Every convenient per-filing source is closed: the old S3 mirror is retired,
ProPublica's API carries no schedules and its download endpoints are
bot-walled, and its HTML schedule pages render client-side. The IRS bulk
corpus is the only path — but it is targeted, not brute force:

  1. the annual index CSV (~90MB) maps EIN -> object_id -> which zip
  2. only the zips containing our filings are fetched
  3. each zip is *streamed* and abandoned the moment its targets are found —
     the Brookings filing was reached after 166MB of a 497MB archive

The zips use Deflate64 (method 9), which Python's zipfile, bsdtar/libarchive
and macOS ditto all refuse — bsdtar silently writes a zero-byte file, which
reads exactly like success. Hence stream-unzip, which handles it and streams.

DATABASE_URL note: this script prints the host it connected to. A sibling
script once reported "nothing to repair" while pointed at the wrong database
(see sift/STATUS.md); an ingest that cannot say where it wrote is not
trustworthy.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import logging
import os
import sys
import tempfile
import urllib.request
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from services.funding_edges import (  # noqa: E402
    FundingEdge,
    NameVerdict,
    apply_verdicts,
    load_ein_index,
    parse_filing,
)

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger("ingest_funding_edges")

IRS_BASE = "https://apps.irs.gov/pub/epostcard/990/xml"
UA = {"User-Agent": "sift-api funding-edge ingest (research)"}

#: The slice: spectrum-balanced think tanks and advocacy orgs that already have
#: Sift dossiers and recoverable EINs. Ten is deliberate — the fuzzy direction
#: of this graph (foundation -> grantee, name-only on 990-PF) is hand-checkable
#: at ten and unmanageable at a hundred.
ORGS: dict[str, str] = {
    "237327730": "Heritage Foundation",
    "530196577": "Brookings Institution",
    "530218495": "American Enterprise Institute",
    "237432162": "Cato Institute",
    "300126510": "Center for American Progress",
    "521368964": "Economic Policy Institute",
    "363235550": "Federalist Society",
    "132912529": "Manhattan Institute",
    "237213592": "Roosevelt Institute",
    "522313694": "American Constitution Society",
}


def _download(url: str, dest: Path) -> Path:
    if dest.exists() and dest.stat().st_size > 1_000_000:
        logger.info("cached %s", dest.name)
        return dest
    logger.info("downloading %s", url)
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=900) as r, open(dest, "wb") as f:
        while chunk := r.read(1 << 20):
            f.write(chunk)
    return dest


def select_filings(index_path: Path, only: set[str] | None = None) -> dict[str, dict]:
    """Latest form-990 filing per org. Skips 990-T (no Schedules I/R).

    A filer's most recent return in a given index year is often the 990-T
    (unrelated business income), which carries neither schedule — selecting
    "latest of any type" silently yields an org with zero edges.
    """
    targets = {e for e in ORGS if not only or e in only}
    rows: dict[str, list[dict]] = defaultdict(list)
    with open(index_path, newline="", encoding="utf-8", errors="replace") as f:
        for row in csv.DictReader(f):
            if row["EIN"] in targets and row["RETURN_TYPE"] == "990":
                rows[row["EIN"]].append(row)
    selected = {}
    for ein, candidates in rows.items():
        candidates.sort(key=lambda r: (r["TAX_PERIOD"], r["SUB_DATE"]), reverse=True)
        selected[ein] = candidates[0]
    for ein in targets:
        if ein not in selected:
            logger.warning(
                "no form-990 row for %s (%s) in this index year", ORGS[ein], ein
            )
    return selected


def fetch_filings(selected: dict[str, dict], year: str, cache: Path) -> dict[str, str]:
    """object_id -> XML text, streaming each zip only as far as needed."""
    from stream_unzip import stream_unzip  # dev-only dependency

    wanted: dict[str, dict[str, str]] = defaultdict(dict)
    for ein, row in selected.items():
        wanted[row["XML_BATCH_ID"]][row["OBJECT_ID"]] = ein

    out: dict[str, str] = {}
    for batch, targets in sorted(wanted.items()):
        cached = {oid: cache / f"{oid}.xml" for oid in targets}
        if all(p.exists() and p.stat().st_size > 0 for p in cached.values()):
            for oid, p in cached.items():
                out[oid] = p.read_text(encoding="utf-8", errors="replace")
            logger.info("%s: all targets cached", batch)
            continue

        url = f"{IRS_BASE}/{year}/{batch}.zip"
        logger.info("%s: streaming %s", batch, url)
        remaining = dict(targets)
        streamed = 0

        # `url` is bound as a default rather than captured: the generator is
        # consumed in this same iteration today, but a late-binding closure
        # over a loop variable is one refactor away from streaming the wrong
        # archive silently (ruff B023).
        def chunks(url=url):
            nonlocal streamed
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=1800) as r:
                while chunk := r.read(1 << 18):
                    streamed += len(chunk)
                    yield chunk

        for name, _size, member in stream_unzip(chunks()):
            fname = name.decode("utf-8", "replace") if isinstance(name, bytes) else name
            hit = next((oid for oid in remaining if oid in fname), None)
            if hit:
                data = b"".join(member).decode("utf-8", "replace")
                (cache / f"{hit}.xml").write_text(data, encoding="utf-8")
                out[hit] = data
                del remaining[hit]
                if not remaining:
                    logger.info("  found all targets after %.0fMB", streamed / 1048576)
                    break
            else:
                for _ in member:
                    pass
        if remaining:
            logger.warning("  %s: not found in archive: %s", batch, list(remaining))
    return out


def build_edges(
    selected: dict[str, dict], filings: dict[str, str], ein_index: dict[str, set[str]]
) -> list[FundingEdge]:
    edges: list[FundingEdge] = []
    for ein, row in selected.items():
        xml = filings.get(row["OBJECT_ID"])
        if not xml:
            logger.warning("no XML for %s (%s)", ORGS[ein], ein)
            continue
        edges.extend(
            parse_filing(
                xml,
                source_ein=ein,
                source_name=ORGS[ein],
                fiscal_period=row["TAX_PERIOD"],
                object_id=row["OBJECT_ID"],
                filing_url=(
                    "https://projects.propublica.org/nonprofits/organizations/"
                    f"{ein}/{row['OBJECT_ID']}/IRS990"
                ),
            )
        )
    return apply_verdicts(edges, ein_index)


async def persist(edges: list[FundingEdge]) -> int:
    import asyncpg

    db_url = os.environ.get("DATABASE_URL")
    if not db_url:
        raise SystemExit("DATABASE_URL not set — refusing to guess a database.")
    host = db_url.split("@")[-1].split("/")[0]
    logger.info("connecting to %s", host)  # never silently write to the wrong DB
    conn = await asyncpg.connect(db_url)
    try:
        written = 0
        for e in edges:
            written += bool(
                await conn.execute(
                    """
                    INSERT INTO funding_edges (
                        source_ein, source_name, target_ein, target_name_as_filed,
                        target_name_irs, edge_kind, amount_usd, purpose, exempt_code,
                        fiscal_period, form, ein_name_agrees, object_id, filing_url
                    ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14)
                    ON CONFLICT DO NOTHING
                    """,
                    e.source_ein, e.source_name, e.target_ein, e.target_name_as_filed,
                    e.target_name_irs, e.edge_kind, e.amount_usd, e.purpose,
                    e.exempt_code, e.fiscal_period, e.form, e.verdict.value,
                    e.object_id, e.filing_url,
                )
            )
        return written
    finally:
        await conn.close()


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--year", default="2025", help="IRS index year (default 2025)")
    ap.add_argument("--apply", action="store_true", help="write to DATABASE_URL")
    ap.add_argument("--dry-run", action="store_true", help="parse and report only")
    ap.add_argument("--cache", default=None, help="dir for index + filing cache")
    ap.add_argument(
        "--only",
        default=None,
        help="comma-separated EINs to ingest (default: all of ORGS). A filing "
        "year holds different orgs' returns, so this also keeps a re-run from "
        "streaming archives it does not need.",
    )
    args = ap.parse_args()
    if not args.apply and not args.dry_run:
        ap.error("pass --dry-run or --apply")
    only = {e.strip() for e in args.only.split(",")} if args.only else None

    cache = Path(args.cache or (Path(tempfile.gettempdir()) / "sift-990-cache"))
    cache.mkdir(parents=True, exist_ok=True)

    index_path = _download(
        f"{IRS_BASE}/{args.year}/index_{args.year}.csv",
        cache / f"index_{args.year}.csv",
    )
    selected = select_filings(index_path, only)
    logger.info("selected %d/%d filings", len(selected), len(only or ORGS))

    filings = fetch_filings(selected, args.year, cache)
    ein_index = load_ein_index([str(index_path)])
    edges = build_edges(selected, filings, ein_index)

    counts: dict[str, int] = defaultdict(int)
    for e in edges:
        counts[e.verdict.value] += 1
    total = sum(e.amount_usd or 0 for e in edges)
    print(f"\n{len(edges)} edges | ${total:,} cash")
    for verdict in NameVerdict:
        print(f"  {verdict.value:11} {counts[verdict.value]}")

    held = [e for e in edges if e.verdict is NameVerdict.REVIEW]
    if held:
        print("\nheld for review — filed name disagrees with the IRS record:")
        for e in held:
            print(f"  EIN {e.target_ein}  filed: {e.target_name_as_filed}")
            print(f"                 IRS: {e.target_name_irs}   ({e.source_name})")

    if args.dry_run:
        print("\ndry run — nothing written")
        return
    written = asyncio.run(persist(edges))
    print(f"\ninserted {written} new rows ({len(edges) - written} already present)")


if __name__ == "__main__":
    main()
