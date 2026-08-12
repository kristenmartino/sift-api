"""Adjudicate funding edges the automated EIN-name check held back.

    python scripts/review_funding_edges.py --list
    python scripts/review_funding_edges.py --confirm 42 --note "Harvard Law is inside the College's entity" --by kristen
    python scripts/review_funding_edges.py --reject 17 --note "wrong EIN; recipient is Urban League of Louisiana"

Migration 027 made only `ein_name_agrees = 'agrees'` publishable. That gate is
right and was incomplete: a held edge had no way to become unheld, so a
legitimate one (Harvard Law School filed under President and Fellows of
Harvard College) would sit withheld forever.

The decision is recorded as a separate layer, never as an overwrite of the
machine verdict. Afterwards you can still see both that the check fired and
that a person disagreed — which is the interesting fact, and the one an
overwrite would destroy.

Rejected edges are kept, not deleted. A filer misfiling a grant is itself a
finding, and deleting the row invites the next re-ingest to resurrect it
silently.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import settings  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger("review_funding_edges")


def _db_url() -> str:
    url = os.environ.get("DATABASE_URL", settings.database_url)
    if not url:
        raise SystemExit("No DATABASE_URL in the environment or .env.")
    host = url.split("@")[-1].split("/")[0]
    logger.info("connecting to %s", host)
    if "localhost" in host or "127.0.0.1" in host:
        logger.warning(
            "resolved to a LOCAL database (%s). Set DATABASE_URL explicitly if "
            "you meant production.",
            host,
        )
    return url


async def list_held(show_all: bool) -> None:
    import asyncpg

    conn = await asyncpg.connect(_db_url())
    try:
        rows = await conn.fetch(
            """
            SELECT id, source_name, target_name_as_filed, target_name_irs,
                   target_ein, edge_kind, amount_usd, ein_name_agrees,
                   review_decision, review_note, reviewed_by
            FROM funding_edges
            WHERE ein_name_agrees <> 'agrees'
              AND ($1 OR review_decision IS NULL)
            ORDER BY ein_name_agrees, source_name, amount_usd DESC NULLS LAST
            """,
            show_all,
        )
        if not rows:
            print("Nothing held." if not show_all else "No held edges at all.")
            return
        current = None
        for r in rows:
            if r["ein_name_agrees"] != current:
                current = r["ein_name_agrees"]
                header = {
                    "review": "NAME DISAGREES WITH THE IRS RECORD — needs a decision",
                    "ein_absent": "EIN NOT IN THE FILER INDEX — usually an LLC or "
                    "a non-filer; confirm only if you can verify it another way",
                }.get(current, current)
                print(f"\n=== {header} ===")
            amt = f"${r['amount_usd']:,}" if r["amount_usd"] else r["edge_kind"]
            print(f"  [{r['id']}] {r['source_name']} -> {r['target_name_as_filed']}  {amt}")
            print(f"        EIN {r['target_ein']}   IRS says: {r['target_name_irs'] or '(no record)'}")
            if r["review_decision"]:
                print(
                    f"        DECIDED: {r['review_decision']} by {r['reviewed_by']}"
                    f" — {r['review_note']}"
                )
        print(f"\n{len(rows)} edge(s).")
    finally:
        await conn.close()


async def decide(edge_id: int, decision: str, note: str, by: str) -> None:
    import asyncpg

    conn = await asyncpg.connect(_db_url())
    try:
        row = await conn.fetchrow(
            "SELECT source_name, target_name_as_filed, target_name_irs, "
            "target_ein, ein_name_agrees FROM funding_edges WHERE id = $1",
            edge_id,
        )
        if row is None:
            raise SystemExit(f"No edge with id {edge_id}.")
        if row["ein_name_agrees"] == "agrees":
            raise SystemExit(
                f"Edge {edge_id} already passes the automated check; nothing to decide."
            )
        print(f"\n  {row['source_name']} -> {row['target_name_as_filed']}")
        print(f"  EIN {row['target_ein']}   IRS says: {row['target_name_irs'] or '(no record)'}")
        print(f"  verdict: {row['ein_name_agrees']}  ->  decision: {decision}\n")
        await conn.execute(
            """
            UPDATE funding_edges
               SET review_decision = $2, review_note = $3,
                   reviewed_by = $4, reviewed_at = NOW()
             WHERE id = $1
            """,
            edge_id,
            decision,
            note,
            by,
        )
        print(
            f"recorded: {decision} by {by}"
            + ("  (this edge will now publish)" if decision == "confirmed" else "")
        )
    finally:
        await conn.close()


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--list", action="store_true", help="show edges awaiting a decision")
    ap.add_argument("--all", action="store_true", help="with --list, include decided ones")
    ap.add_argument("--confirm", type=int, metavar="ID", help="the filed name really is this EIN")
    ap.add_argument("--reject", type=int, metavar="ID", help="genuinely wrong; never publish")
    ap.add_argument("--note", default="", help="why — recorded with the decision")
    ap.add_argument("--by", default=os.environ.get("USER", "unknown"), help="who decided")
    args = ap.parse_args()

    if args.confirm and args.reject:
        ap.error("pick one of --confirm / --reject")
    if args.confirm or args.reject:
        if not args.note:
            # A decision without a reason is an unexplained override of a
            # check that exists to catch exactly this kind of judgement call.
            ap.error("--note is required: record why you decided this")
        edge_id = args.confirm or args.reject
        decision = "confirmed" if args.confirm else "rejected"
        asyncio.run(decide(edge_id, decision, args.note, args.by))
        return
    if args.list:
        asyncio.run(list_held(args.all))
        return
    ap.error("pass --list, --confirm ID or --reject ID")


if __name__ == "__main__":
    main()
