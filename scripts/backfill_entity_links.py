"""Backfill articles.entity_links — re-run the linker over stored articles.

Originally a one-shot after PR #40 dropped last-name-only aliases (50 stored
articles needed re-linking; 46 cleared to []). Updated for Phase 3.G.2 to use
the same `link_articles`-style LLM path the pipeline node uses.

Widened 2026-08-05 to reach articles that have **no** links at all.

Why that matters: the audit (scripts/audit_unlinked_entities.py) found
entity_links on only 21,614 of 282,931 articles — 7.6%. Migration 014 added
curated aliases ("Pentagon" → united-states-department-of-defense, "Trump" →
EXEC-TRUMP-DJ), but the old selection here — `entity_links != '[]'` — could
only ever re-link the 7.6% that already had chips. The 261,317 articles the
alias work was meant to fix were the exact rows it skipped.

Two things follow, and they set the shape of this script:

**Cost.** The LLM path runs ~$0.001/article. Over the empty set that is
~$260, against a documented ~$10/day AI budget, with AI_COST_GUARD_ENABLED
off in prod. So `--include-empty` defaults to `--mode regex`, which is free,
deterministic, and captures exactly the alias win: aliases live in
build_search_dict, so `link_text` resolves them. LLM mode over a large set
requires an explicit `--yes`.

**Scale.** 283k rows do not belong in memory at once, and a run that loses
everything to a Ctrl-C is unusable. Ids are fetched first, then processed in
chunks with a write per chunk. The script is idempotent, so an interrupted
run is resumed by re-running it.

**Behavior change to know about.** Selection is unchanged by default (the
already-linked rows, LLM path), but the cost gate is new and now applies
there too: 21,614 rows is ~$22, so an argument-free run refuses until you
pass --yes. It used to spend that silently. --dry-run does not exempt it —
dry-run skips the writes, not the API calls.

Regex mode is additive only. It never removes a stored link, because it is
strictly weaker than the LLM path: on a 5,000-article sample it would have
cleared 56 rows the LLM had linked. Clearing a bad link is what --mode llm
is for.

LLM mode overwrites, so it only writes rows the model actually answered for.
A failed call (timeout, API error, garbled response) leaves the stored links
untouched and is counted as `skipped` — re-run to retry it. Conflating the
two is not hypothetical: on 2026-08-05 a scoped 296-article run hit a wave of
8s timeouts, read them as "no entities", and emptied 218 rows, at least 34 of
which plainly named catalog entities. They were restored with an additive
regex pass. See services/entity_linker_llm.link_text_llm for the [] vs None
contract this depends on.

Usage (from sift-api root):

    # the historical pass, now explicit about its cost
    railway run ./.venv/bin/python3 scripts/backfill_entity_links.py --yes

    # the wide, free pass: apply curated aliases to everything
    railway run ./.venv/bin/python3 scripts/backfill_entity_links.py \\
        --include-empty --dry-run
    railway run ./.venv/bin/python3 scripts/backfill_entity_links.py --include-empty

    # bound a first run
    railway run ./.venv/bin/python3 scripts/backfill_entity_links.py \\
        --include-empty --limit 5000

Idempotent. Only writes a row when the new value differs from the stored one.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys

# scripts/ sits next to services/ — make the latter importable when run
# from any cwd.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import asyncpg  # noqa: E402

from services.entity_linker import (  # noqa: E402
    build_catalog,
    build_search_dict,
    link_text,
)
from services.entity_linker_llm import link_articles_llm  # noqa: E402

# Rough per-article cost of the LLM path with prompt caching, from this
# script's original Phase 3.G.2 note. Used only to refuse an expensive run
# before it starts — not billing-accurate.
_EST_LLM_COST_PER_ARTICLE = 0.001

# Above this many articles, LLM mode demands --yes.
_LLM_CONFIRM_THRESHOLD = 1_000


async def _load_catalog(conn: asyncpg.Connection) -> list:
    """Same four queries the pipeline node uses, plus curated aliases.

    The alias fetch is the point of the widened backfill: without it this
    script rebuilds the pre-014 catalog and the run is a no-op for exactly
    the surface forms it exists to apply.
    """
    outlets = [dict(r) for r in await conn.fetch("SELECT slug, name FROM outlet_profiles")]
    politicians = [dict(r) for r in await conn.fetch(
        "SELECT bioguide_id, name FROM politician_profiles"
    )]
    orgs = [dict(r) for r in await conn.fetch("SELECT slug, name FROM org_profiles")]
    bills = [dict(r) for r in await conn.fetch(
        "SELECT bill_id, title, short_title FROM bill_profiles"
    )]
    try:
        aliases = [dict(r) for r in await conn.fetch(
            "SELECT alias, entity_type, canonical_id FROM entity_aliases"
        )]
    except asyncpg.UndefinedTableError:
        print("  note: entity_aliases absent (pre-014 DB) — canonical names only")
        aliases = []

    catalog = build_catalog(outlets, politicians, orgs, bills, aliases)
    print(
        f"Catalog: {len(outlets)} outlets, {len(politicians)} politicians, "
        f"{len(orgs)} orgs, {len(bills)} bills, {len(aliases)} aliases "
        f"→ {len(catalog)} entries"
    )
    return catalog


async def _self_outlet_map(conn: asyncpg.Connection) -> dict[str, str]:
    """{lowercased source_name: outlet_slug} for self-reference suppression.

    The LLM path drops a chip pointing at the article's own publisher; the
    regex path has no source_name awareness at all, so without this a wide
    regex pass would stamp a Fox News chip on every Fox News article that
    happens to say "Fox News". Built from source_name_aliases plus an exact
    name match, mirroring how the feed resolves provenance.
    """
    out: dict[str, str] = {}
    for r in await conn.fetch("SELECT slug, name FROM outlet_profiles"):
        if r["name"]:
            out[r["name"].strip().lower()] = r["slug"]
    try:
        for r in await conn.fetch(
            "SELECT raw_source_name, outlet_slug FROM source_name_aliases"
        ):
            if r["raw_source_name"]:
                out[r["raw_source_name"].strip().lower()] = r["outlet_slug"]
    except asyncpg.UndefinedTableError:
        pass
    return out


def _regex_link(
    article: dict,
    search_dict: dict,
    self_outlets: dict[str, str],
) -> list[dict]:
    """Deterministic link pass for one article, with self-outlet suppression."""
    text = f"{article['title']}\n{article['summary']}"
    links = link_text(text, search_dict)
    own = self_outlets.get((article.get("source_name") or "").strip().lower())
    if own:
        links = [
            link for link in links
            if not (link["type"] == "outlet" and link["canonical_id"] == own)
        ]
    return links


def _merge_additive(old_json: str, new_links: list[dict]) -> list[dict]:
    """Union stored links with regex-found ones — never remove.

    Regex is strictly weaker than the LLM path: on a 5,000-article sample it
    cleared 56 rows the LLM had linked. Overwriting there would trade good
    chips for worse ones, so regex mode is additive only and the authoritative
    pass that *can* clear a link stays with --mode llm. An entity already
    present keeps its stored surface_form.
    """
    try:
        old = json.loads(old_json) or []
    except (TypeError, ValueError):
        old = []
    if not isinstance(old, list):
        old = []

    merged = [x for x in old if isinstance(x, dict)]
    seen = {(x.get("type"), x.get("canonical_id")) for x in merged}
    for link in new_links:
        key = (link["type"], link["canonical_id"])
        if key not in seen:
            seen.add(key)
            merged.append(dict(link))
    # Same ordering link_text emits, so the JSONB reads consistently.
    merged.sort(key=lambda x: (x.get("type") or "", x.get("canonical_id") or ""))
    return merged


# Retried once on a dropped connection. asyncpg raises these when the server
# closed the session underneath us — distinct from a query error, and safe to
# retry because both operations are idempotent: the read has no side effect and
# the write is a deterministic UPDATE by primary key.
_TRANSIENT = (
    asyncpg.exceptions.ConnectionDoesNotExistError,
    asyncpg.exceptions.InterfaceError,
    ConnectionResetError,
    OSError,
)


async def _fetch_chunk(pool: asyncpg.Pool, batch_ids: list[str]) -> list:
    for attempt in (1, 2):
        try:
            return await pool.fetch(
                """
                SELECT id, title, summary, source_url, source_name,
                       entity_links::text AS el
                FROM articles WHERE id = ANY($1::text[])
                """,
                batch_ids,
            )
        except _TRANSIENT:
            if attempt == 2:
                raise
            print("  (connection dropped on read — reconnecting)", flush=True)
    return []


async def _write_chunk(pool: asyncpg.Pool, writes: list[tuple[str, str]]) -> None:
    for attempt in (1, 2):
        try:
            async with pool.acquire() as conn, conn.transaction():
                await conn.executemany(
                    "UPDATE articles SET entity_links = $1::jsonb WHERE id = $2",
                    writes,
                )
            return
        except _TRANSIENT:
            if attempt == 2:
                raise
            print("  (connection dropped on write — reconnecting)", flush=True)


async def main(
    dry_run: bool,
    include_empty: bool,
    mode: str,
    limit: int | None,
    chunk_size: int,
    assume_yes: bool,
    only_canonical: list[str] | None = None,
) -> int:
    db_url = os.environ.get("DATABASE_URL")
    if not db_url:
        print("ERROR: DATABASE_URL not set.", file=sys.stderr)
        return 1

    ssl = "require" if "neon.tech" in db_url else False
    # A pool, not a single connection. A full-corpus run holds the client open
    # for ~an hour, and Neon closes an idle-ish session well before that: a
    # 2026-08-05 run died at 180,000/284,540 with ConnectionDoesNotExistError
    # mid-transaction. The pool re-establishes on acquire, so a server-side
    # close between chunks costs nothing.
    pool = await asyncpg.create_pool(db_url, ssl=ssl, min_size=1, max_size=3)
    try:
        catalog = await _load_catalog(pool)
        search_dict = build_search_dict(catalog) if mode == "regex" else {}
        self_outlets = await _self_outlet_map(pool) if mode == "regex" else {}
        if mode == "regex":
            print(f"Regex mode: {len(search_dict)} search keys, "
                  f"{len(self_outlets)} source_name → outlet mappings")

        # Selection. The default is the historical one — already-linked rows
        # only — so an argument-free run behaves exactly as it did before.
        where = "entity_links IS NOT NULL AND entity_links::text != '[]'"
        if include_empty:
            where = "TRUE"
        params: list = []
        # --only-canonical narrows to rows carrying a specific chip. Regex mode
        # is additive and can never clear one, so repairing a canonical_id that
        # should not have been linked is necessarily an LLM-mode job — and
        # that is only affordable when it is scoped to the affected rows
        # rather than the whole already-linked set.
        if only_canonical:
            params.append(list(only_canonical))
            where += (
                f" AND EXISTS (SELECT 1 FROM jsonb_array_elements(entity_links) e"
                f" WHERE e->>'canonical_id' = ANY(${len(params)}::text[]))"
            )
        sql = f"SELECT id FROM articles WHERE {where} ORDER BY created_at DESC"
        if limit:
            sql += f" LIMIT {int(limit)}"
        ids = [r["id"] for r in await pool.fetch(sql, *params)]

        total_all = await pool.fetchval("SELECT COUNT(*) FROM articles")
        scope = "all rows" if include_empty else "already-linked rows only"
        if only_canonical:
            scope += f"; carrying {', '.join(sorted(only_canonical))}"
        print(f"Selected {len(ids):,} of {total_all:,} articles ({scope})")

        if not ids:
            print("Nothing to do.")
            return 0

        # Deliberately NOT bypassed by --dry-run: dry-run suppresses the DB
        # writes, not the API calls, so an unguarded `--mode llm --dry-run`
        # over the full corpus would spend the money and save nothing.
        if mode == "llm" and len(ids) > _LLM_CONFIRM_THRESHOLD and not assume_yes:
            est = len(ids) * _EST_LLM_COST_PER_ARTICLE
            print()
            print(f"REFUSING: LLM mode over {len(ids):,} articles is roughly "
                  f"${est:,.0f} at ~${_EST_LLM_COST_PER_ARTICLE}/article.")
            print("(--dry-run does not avoid this: it skips the writes, not the calls.)")
            print("That is far above the documented ~$10/day AI budget, and")
            print("AI_COST_GUARD_ENABLED is off in prod. Either use --mode regex")
            print("(free, and what applies the curated aliases), bound the run with")
            print("--limit, or pass --yes if the spend is intended.")
            return 2

        updated = cleared = added = no_change = failed = 0
        for start in range(0, len(ids), chunk_size):
            batch_ids = ids[start:start + chunk_size]
            rows = await _fetch_chunk(pool, batch_ids)
            articles = [
                {"source_url": r["source_url"], "source_name": r["source_name"],
                 "title": r["title"] or "", "summary": r["summary"] or ""}
                for r in rows
            ]

            if mode == "llm":
                # omit_failures is load-bearing, not defensive. link_text_llm
                # answers [] for "no catalog entity is mentioned" and None for
                # "the call failed" (timeout, API error, garbled response), and
                # LLM mode overwrites rather than merging. Without this, a batch
                # of 8s timeouts is indistinguishable from a batch of genuinely
                # entity-free articles and clears their stored chips — which is
                # what a scoped 296-article run did on 2026-08-05, emptying 218
                # rows, 34 of them provably wrongly.
                link_map = await link_articles_llm(  # type: ignore[arg-type]
                    articles, catalog, omit_failures=True,
                )
                failed += sum(
                    1 for a in articles
                    if a["source_url"] and a["source_url"] not in link_map
                )
            else:
                link_map = {
                    a["source_url"]: _regex_link(a, search_dict, self_outlets)
                    for a in articles if a["source_url"]
                }

            by_url_id = {r["source_url"]: r["id"] for r in rows}
            by_url_old = {r["source_url"]: (r["el"] or "[]") for r in rows}

            writes: list[tuple[str, str]] = []
            for url, new_links in link_map.items():
                aid = by_url_id.get(url)
                if not aid:
                    continue
                old_json = by_url_old.get(url, "[]")
                if mode == "regex":
                    new_links = _merge_additive(old_json, new_links)
                new_json = json.dumps(new_links, separators=(",", ":"))
                if old_json == new_json:
                    no_change += 1
                    continue
                if not new_links:
                    cleared += 1
                elif old_json == "[]":
                    added += 1
                updated += 1
                writes.append((new_json, aid))

            # Write per chunk so an interrupted run keeps its progress. The
            # script is idempotent, so resuming is just re-running it.
            if writes and not dry_run:
                await _write_chunk(pool, writes)

            done = min(start + chunk_size, len(ids))
            progress = (f"  {done:>7,}/{len(ids):,}  updated={updated:,} "
                        f"newly-linked={added:,} cleared={cleared:,}")
            if mode == "llm":
                progress += f" llm-failed={failed:,}"
            print(progress, flush=True)

        print()
        print(f"  updated:              {updated:,}")
        print(f"    newly linked ([] → chips): {added:,}")
        print(f"    cleared to []:             {cleared:,}")
        print(f"  unchanged:            {no_change:,}")
        if mode == "llm":
            print(f"  skipped (LLM call failed, links left as-is): {failed:,}")
            if failed:
                print("    re-run to retry those rows — the script is idempotent.")
        if dry_run:
            print()
            print("--dry-run set; no DB writes.")
        return 0
    finally:
        await pool.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Re-run the entity linker over stored articles and write "
                    "the corrected entity_links back.",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Compute the diff but don't write to the DB.",
    )
    parser.add_argument(
        "--include-empty", action="store_true",
        help="Also process articles with no links yet — the 92%% the old "
             "selection skipped. Implies --mode regex unless overridden.",
    )
    parser.add_argument(
        "--mode", choices=("llm", "regex"), default=None,
        help="Linker path. Defaults to llm for the narrow set (historical "
             "behavior) and regex for --include-empty (free).",
    )
    parser.add_argument(
        "--limit", type=int, default=None,
        help="Process at most N articles, newest first.",
    )
    parser.add_argument(
        "--chunk-size", type=int, default=500,
        help="Articles per fetch/write batch (default: 500).",
    )
    parser.add_argument(
        "--yes", action="store_true",
        help="Skip the LLM cost confirmation.",
    )
    parser.add_argument(
        "--only-canonical", nargs="+", metavar="ID",
        help="Restrict to articles already carrying one of these canonical_ids. "
             "Use with --mode llm to repair chips regex mode cannot clear.",
    )
    args = parser.parse_args()

    resolved_mode = args.mode or ("regex" if args.include_empty else "llm")

    try:
        sys.exit(asyncio.run(main(
            dry_run=args.dry_run,
            include_empty=args.include_empty,
            mode=resolved_mode,
            limit=args.limit,
            chunk_size=args.chunk_size,
            assume_yes=args.yes,
            only_canonical=args.only_canonical,
        )))
    except KeyboardInterrupt:
        print("\nAborted. Progress up to the last completed chunk was written; "
              "re-run to resume.", file=sys.stderr)
        sys.exit(130)
