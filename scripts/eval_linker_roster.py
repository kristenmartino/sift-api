"""Is the narrowed roster as good as the full one, or just different?

WHY THIS EXISTS
---------------
`services/entity_linker.narrow_catalog` sends the LLM linker only the roster
rows the regex already matched, plus same-surname siblings — ~3 rows instead of
~856, which deletes ~95% of the call's input. The obvious way to check it is to
diff its links against the full-roster path, and that measurement is misleading
on its own.

It read 94.2% recall / 85.3% precision on the first run, which looks like a
straight downgrade until you notice the narrowed path found MORE links than the
full one (95 vs 86). "Precision 85.3%" there does not mean 15% of its links are
wrong; it means 15% of its links are ones the full-roster path did not make.
Whether that is a gain or a regression is exactly the question, and a diff
against a non-ground-truth baseline cannot answer it.

WHAT THIS DOES INSTEAD
----------------------
Runs both paths over the same gated articles, collects only the links they
DISAGREE on, and puts each disagreement to a stronger model (Sonnet, the same
judge tier as services/judge.py) as a blind yes/no: does this article actually
refer to this entity?

Blind matters. The adjudicator sees the article and one candidate entity, never
which path proposed it, and the disagreements are shuffled — so it cannot
systematically favour either side.

That converts the diff into two real precision numbers: of the links only the
narrowed path made, how many are genuinely right, and the same for the full
path. A narrowed path whose unique links are mostly correct is an improvement
even though it "disagrees" with the incumbent.

SHIP BAR — AND THE ONE THIS SCRIPT GOT WRONG FIRST
--------------------------------------------------
The first version of this script scored only the disagreements and shipped on
"narrowed-unique precision >= full-unique precision". That bar passed (50.0% vs
42.9%) while the change was in fact a precision *regression*, because it
ignores volume: the narrowed path produced 20 unique links to the full path's
7, so even at a better rate per unique link it added far more wrong ones in
absolute terms.

The number that decides it is OVERALL precision — correct links over all links
produced — which needs the agreed links priced too, not just the disagreements.
So a random sample of agreements is adjudicated as well, and both paths are
scored end to end.

SHIP BAR: overall precision must not regress, and total correct links must not
fall. A path that finds more correct links AND more wrong ones is a judgement
call, not an automatic pass — entity chips are user-visible, so a wrong one
costs more than a missing one.

Usage (from sift-api root, costs real API money — ~$0.50 at n=200):

    ./.venv/bin/python3 scripts/eval_linker_roster.py
    ./.venv/bin/python3 scripts/eval_linker_roster.py --n 300 --json out.json
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import anthropic  # noqa: E402
import asyncpg  # noqa: E402

from app.config import settings  # noqa: E402
from services.entity_linker import (  # noqa: E402
    build_catalog,
    build_search_dict,
    link_text,
    narrow_catalog,
)
from services import entity_linker_llm as ELL  # noqa: E402

# Same tier as services/judge.py — the adjudicator has to be better than the
# thing it is judging, or it just re-votes for whichever answer Haiku prefers.
JUDGE_MODEL = "claude-sonnet-4-6"
JUDGE_CONCURRENCY = 6

# How many agreed links to price the shared base with. Both paths carry the
# agreements identically, so this only needs to be accurate enough to place
# overall precision — it does not have to be exhaustive.
AGREED_SAMPLE = 60

# The linker's own contract, restated so the judge scores against the rules the
# linker was asked to follow rather than its own idea of a mention. Condensed
# from SYSTEM_INSTRUCTIONS in services/entity_linker_llm.py — keep in sync.
JUDGE_RULES = """Rules (these are the rules the tagger was given):
- A politician counts only if the article names that specific person directly \
(full name, or a clearly resolvable form like "Speaker Johnson", "Sen. Schumer"). \
A state name, party label, or chamber alone is NOT a reference to any politician.
- Names overlap in public life. If the article's person is a DIFFERENT individual \
who happens to share the name, answer no.
- An outlet counts only if the article names it as a source of reporting \
("according to Reuters"). An article's own publisher does not count.
- An org or bill counts only if the article actually refers to it."""


async def _load_catalog(conn) -> list[dict]:
    outlets = [dict(r) for r in await conn.fetch("SELECT slug, name FROM outlet_profiles")]
    politicians = [dict(r) for r in await conn.fetch(
        "SELECT bioguide_id, name FROM politician_profiles")]
    orgs = [dict(r) for r in await conn.fetch("SELECT slug, name FROM org_profiles")]
    bills = [dict(r) for r in await conn.fetch(
        "SELECT bill_id, title, short_title FROM bill_profiles")]
    try:
        aliases = [dict(r) for r in await conn.fetch(
            "SELECT alias, entity_type, canonical_id, match_case FROM entity_aliases")]
    except asyncpg.UndefinedTableError:
        aliases = []
    return build_catalog(outlets, politicians, orgs, bills, aliases)


async def _fetch_gated(conn, search_dict, n: int) -> list[dict]:
    """Articles that clear the regex gate — the only ones the LLM ever sees."""
    rows = await conn.fetch(
        """SELECT title, summary, source_url, source_name FROM articles
           WHERE created_at >= CURRENT_DATE - INTERVAL '4 days'
             AND title IS NOT NULL AND summary IS NOT NULL AND summary <> ''
           ORDER BY created_at DESC LIMIT $1""",
        n * 6,
    )
    out = []
    for r in rows:
        a = dict(r)
        if link_text(f"{a['title']}\n{a['summary']}", search_dict):
            out.append(a)
        if len(out) >= n:
            break
    return out


def _key(links) -> set[tuple[str, str]]:
    return {(x["type"], x["canonical_id"]) for x in (links or [])}


async def _judge_one(client, sem, article: dict, ref: tuple[str, str], name: str) -> bool | None:
    etype, cid = ref
    prompt = (
        f"Article source: {article.get('source_name') or 'unknown'}\n"
        f"Article title: {article['title']}\n"
        f"Article summary: {article['summary']}\n\n"
        f"Candidate tag — type: {etype}, name: {name}\n\n"
        f"{JUDGE_RULES}\n\n"
        "Does this article genuinely refer to that specific entity? "
        'Answer with JSON only: {"correct": true} or {"correct": false}.'
    )
    async with sem:
        try:
            resp = await client.messages.create(
                model=JUDGE_MODEL, max_tokens=20,
                messages=[{"role": "user", "content": prompt}],
            )
        except Exception as e:  # noqa: BLE001
            print(f"  judge error ({cid}): {e}", file=sys.stderr)
            return None
    text = "".join(b.text for b in resp.content if b.type == "text")
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1:
        return None
    try:
        return bool(json.loads(text[start:end + 1]).get("correct"))
    except json.JSONDecodeError:
        return None


async def main(n: int, out_json: str | None) -> int:
    url = settings.database_url
    conn = await asyncpg.connect(url, ssl="require" if "neon.tech" in url else False)
    try:
        catalog = await _load_catalog(conn)
        search_dict = build_search_dict(catalog)
        articles = await _fetch_gated(conn, search_dict, n)
    finally:
        await conn.close()

    names = {(r["type"], r["canonical_id"]): r["primary_name"] for r in catalog}
    print(f"catalog={len(catalog)} rows · gated articles={len(articles)}")
    if not articles:
        print("No gated articles in the window.")
        return 1

    client = ELL._client()
    sem = asyncio.Semaphore(4)

    async def _full(a):
        async with sem:
            return await ELL.link_text_llm(
                a["title"], a["summary"], catalog,
                source_name=a.get("source_name"), client=client)

    async def _narrow(a):
        matches = link_text(f"{a['title']}\n{a['summary']}", search_dict)
        cat = narrow_catalog(catalog, matches)
        async with sem:
            return await ELL.link_text_llm(
                a["title"], a["summary"], cat,
                source_name=a.get("source_name"), client=client)

    full = await asyncio.gather(*(_full(a) for a in articles))
    narrow = await asyncio.gather(*(_narrow(a) for a in articles))

    sizes = [len(narrow_catalog(catalog, link_text(
        f"{a['title']}\n{a['summary']}", search_dict))) for a in articles]
    print(f"roster rows per call: full={len(catalog)}  "
          f"narrowed mean={sum(sizes)/len(sizes):.1f} max={max(sizes)}")

    # Every disagreement, plus a sample of agreements. The agreements are what
    # price the shared base: without them there is no overall precision, only a
    # rate on the tail, and the tail is where both paths are least reliable.
    jobs: list[tuple[dict, tuple[str, str], str]] = []
    agreed_refs: list[tuple[dict, tuple[str, str]]] = []
    agreed = 0
    # strict=True: these three come from gather() over the same list, so a
    # length mismatch means results silently drifted out of alignment with the
    # articles they describe — which would misattribute every link after it.
    for a, f, nw in zip(articles, full, narrow, strict=True):
        fk, nk = _key(f), _key(nw)
        agreed += len(fk & nk)
        for ref in sorted(fk & nk):
            agreed_refs.append((a, ref))
        for ref in (fk - nk):
            jobs.append((a, ref, "full"))
        for ref in (nk - fk):
            jobs.append((a, ref, "narrow"))

    # Deterministic sample of the agreements — content-hashed, so a re-run
    # prices the same base and the two paths are never compared across
    # different samples.
    agreed_refs.sort(key=lambda x: hash((x[0]["source_url"], x[1])))
    sampled = agreed_refs[:AGREED_SAMPLE]
    jobs.extend((a, ref, "agreed") for a, ref in sampled)

    f_tot = sum(len(_key(x)) for x in full)
    n_tot = sum(len(_key(x)) for x in narrow)
    print(f"links: full={f_tot} narrowed={n_tot} agreed={agreed} "
          f"disagreements={len(jobs)}")
    if not jobs:
        print("No disagreements to adjudicate.")
        return 0

    # Shuffle so the judge cannot infer the source path from ordering. Seeded
    # by content, not by random(), so a re-run adjudicates in the same order.
    jobs.sort(key=lambda j: hash((j[0]["source_url"], j[1])))

    jclient = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)
    jsem = asyncio.Semaphore(JUDGE_CONCURRENCY)
    verdicts = await asyncio.gather(*(
        _judge_one(jclient, jsem, a, ref, names.get(ref, ref[1]))
        for a, ref, _ in jobs
    ))

    stats = {"full": [0, 0], "narrow": [0, 0], "agreed": [0, 0]}   # [correct, judged]
    wrong: dict[str, list] = {"full": [], "narrow": []}
    for (a, ref, side), v in zip(jobs, verdicts, strict=True):
        if v is None:
            continue
        stats[side][1] += 1
        if v:
            stats[side][0] += 1
        elif side != "agreed" and len(wrong[side]) < 5:
            wrong[side].append((a["title"][:56], ref[1]))

    print("\nBLIND ADJUDICATION (Sonnet; path hidden, order content-hashed)")
    print(f"{'bucket':>10} {'judged':>7} {'correct':>8} {'precision':>10}")
    result = {}
    for side in ("agreed", "full", "narrow"):
        c, j = stats[side]
        pct = 100.0 * c / j if j else 0.0
        label = {"agreed": "both", "full": "full only", "narrow": "narrow only"}[side]
        result[side] = {"correct": c, "judged": j, "precision": pct}
        print(f"{label:>10} {j:>7} {c:>8} {pct:>9.1f}%")

    for side in ("full", "narrow"):
        if wrong[side]:
            print(f"\n  judged WRONG, unique to {side}:")
            for title, cid in wrong[side]:
                print(f"    {cid:24} {title!r}")

    # Overall precision. The agreed base is priced from its sample and applied
    # to all `agreed` links, which both paths carry identically; each path then
    # adds its own uniques at their measured rate.
    base_rate = (result["agreed"]["correct"] / result["agreed"]["judged"]
                 if result["agreed"]["judged"] else 0.0)
    base_correct = base_rate * agreed

    def _overall(side: str, total: int) -> tuple[float, float]:
        r = result[side]
        uniq_rate = (r["correct"] / r["judged"]) if r["judged"] else 0.0
        uniq_n = total - agreed
        correct = base_correct + uniq_rate * uniq_n
        return correct, (100.0 * correct / total if total else 0.0)

    f_correct, f_prec = _overall("full", f_tot)
    n_correct, n_prec = _overall("narrow", n_tot)

    print(f"\nOVERALL (agreed base priced at {100 * base_rate:.1f}% and applied to both)")
    print(f"{'path':>10} {'links':>7} {'est. correct':>13} {'precision':>10}")
    print(f"{'full':>10} {f_tot:>7} {f_correct:>13.1f} {f_prec:>9.1f}%")
    print(f"{'narrowed':>10} {n_tot:>7} {n_correct:>13.1f} {n_prec:>9.1f}%")

    prec_ok = n_prec >= f_prec
    recall_ok = n_correct >= f_correct
    verdict = "PASS" if (prec_ok and recall_ok) else "FAIL"
    print("\nSHIP BAR: overall precision must not regress AND correct links must not fall")
    print(f"  precision {n_prec:.1f}% vs {f_prec:.1f}%  {'ok' if prec_ok else 'REGRESSION'}")
    print(f"  correct   {n_correct:.1f} vs {f_correct:.1f}  {'ok' if recall_ok else 'REGRESSION'}")
    print(f"  {verdict}")

    if out_json:
        os.makedirs(os.path.dirname(out_json) or ".", exist_ok=True)
        with open(out_json, "w") as fh:
            json.dump({"articles": len(articles), "links_full": f_tot,
                       "links_narrow": n_tot, "agreed": agreed,
                       "roster_mean": sum(sizes) / len(sizes),
                       "adjudicated": result, "base_rate": base_rate,
                       "overall": {"full": {"correct": f_correct, "precision": f_prec},
                                   "narrow": {"correct": n_correct, "precision": n_prec}},
                       "verdict": verdict}, fh, indent=2)
        print(f"wrote {out_json}")

    return 0 if verdict == "PASS" else 2


if __name__ == "__main__":
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--n", type=int, default=200, help="gated articles to sample")
    p.add_argument("--json", dest="out_json", help="also write the report here")
    args = p.parse_args()
    sys.exit(asyncio.run(main(args.n, args.out_json)))
