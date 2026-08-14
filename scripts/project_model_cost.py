#!/usr/bin/env python3
"""What each pipeline stage would cost on a different model, without spending.

WHY THIS EXISTS
---------------
Haiku 4.5 is the cheapest Claude there is, so a cost-motivated model change
means leaving Anthropic for some stages. That is a real experiment with a real
quality risk, and which candidates are worth paying to evaluate has to be
settled by arithmetic first. This script is that shortlist step.

It also settles which STAGES are worth evaluating, and the first measured run
overturned the prior guess. The plan this came from assumed a candidate saves
some fraction and concluded five of eight stages could never repay their own
eval, each capped under $210/yr. Measured against a 93%-cheaper candidate the
floor is ~$103/yr and three stages clear $200 — synthesizer $440, summarizer
$295, primer $203. Every stage is worth evaluating; none is written off.

WHY IT COULD NOT EXIST UNTIL NOW
--------------------------------
`ai_usage_daily` recorded dollars and call counts and nothing else, so cost was
one equation with two unknowns:

    cost = input_tokens x price_in + output_tokens x price_out

A stored dollar figure cannot be re-priced against another model's rates —
infinitely many token splits produce the same cost. migrations/029 added the
token columns; this reads them.

`services/cost_estimates.py` currently bounds an unmeasured model by the worse
of its two price ratios, which is deliberately conservative because it had no
split to work from. Once a stage shows up here with real tokens, that bound can
be replaced by its projection.

WHY THE RATIO IS THE POINT
--------------------------
Haiku prices output at 5x input, and the stages sit at opposite ends of the
ratio: `entity_linker_llm` sends a roster and gets back a short list;
`story_synthesizer` sends titles and gets back prose. A candidate that is
cheap on input and dear on output saves far less on synthesis than a
list-price comparison suggests, and the effect is per-stage rather than
uniform. That is the whole reason to project per operation instead of applying
one multiplier to the bill.

WHAT IT DOES NOT DO
-------------------
It does not model quality, latency, or the 50% Anthropic Batch API discount
disappearing on a provider that has no batch API — three stages depend on that
discount, so their real comparison is 2x what a token projection alone shows.
`supports_batch` in services/model_registry.py is the flag; this script flags
those rows rather than silently understating them.

Usage:
    railway run ./.venv/bin/python3 scripts/project_model_cost.py --days 1
    railway run ./.venv/bin/python3 scripts/project_model_cost.py --days 7 --json out.json
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from dataclasses import dataclass

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import asyncpg  # noqa: E402

from services import usage_tracker  # noqa: E402
from services.model_registry import (  # noqa: E402
    CAPABILITIES,
    BATCH,
    spec_for_wire_model,
)


@dataclass(frozen=True)
class Candidate:
    """A model to price against, and whether it can do what the stage needs."""

    label: str
    input_per_m: float
    output_per_m: float
    has_batch_discount: bool
    source: str = ""  # where the price came from, so it can be re-checked
    note: str = ""


# Prices retrieved 2026-08-13 from each vendor's own pricing page. RE-CHECK
# BEFORE QUOTING — these move, and a stale table here produces confident
# arithmetic on numbers that are no longer real.
PRICES_RETRIEVED = "2026-08-13"


# Anthropic rows come from usage_tracker.PRICES so they cannot drift from what
# the ledger is actually charged at. Non-Anthropic rows are placeholders with
# their prices stated explicitly rather than imported, because nothing in this
# repo calls them yet — fill in real figures before quoting any output.
#
# Deliberately NOT hardcoding vendor names with invented prices: a plausible
# number in a table like this gets quoted, and this program's whole failure
# mode is producing confident arithmetic on made-up inputs.
def _anthropic(label: str, model: str) -> Candidate:
    """Anthropic rows come from usage_tracker.PRICES so they cannot drift from
    what the ledger is actually charged at."""
    p = usage_tracker.prices_for(model)
    return Candidate(
        label, p.input_per_m, p.output_per_m, has_batch_discount=True,
        source="services/usage_tracker.PRICES",
    )


CANDIDATES: list[Candidate] = [
    _anthropic("haiku-4-5 (incumbent)", "claude-haiku-4-5-20251001"),
    _anthropic("sonnet-4-6", "claude-sonnet-4-6"),

    # ── closed budget tiers ──────────────────────────────────
    # Both have a real Batch API at 50%, so unlike the open-weight rows below
    # they do NOT surrender the discount on context/primer/entity_extractor.
    # They would still need those three paths rewritten — their batch APIs are
    # file-upload-plus-batch-object shaped, not `messages.batches` — but that
    # is an engineering cost, not a price one.
    Candidate("gpt-5-nano", 0.05, 0.40, True,
              source="developers.openai.com/api/docs/pricing"),
    Candidate("gemini-2.5-flash-lite", 0.10, 0.40, True,
              source="ai.google.dev/gemini-api/docs/pricing"),

    # ── open-weight, hosted ──────────────────────────────────
    # gpt-oss-120b is quoted identically by Groq and Together ($0.15/$0.60).
    # Together lists no batch discount for it; Groq's model docs do not state
    # one either, so it is priced here WITHOUT a discount. If Groq's batch
    # discount is confirmed, these three stages get cheaper, not dearer — the
    # conservative direction.
    Candidate("gpt-oss-120b (Groq/Together)", 0.15, 0.60, False,
              source="console.groq.com/docs/models + together.ai/pricing",
              note="no batch discount confirmed"),
    # The cheapest credible output price found. Together lists a batch
    # discount for it.
    Candidate("DeepSeek V4 Flash (Together)", 0.14, 0.28, True,
              source="together.ai/pricing"),

    # ── Kimi, priced because it gets asked about ─────────────
    # It is the counter-example that makes the output-price finding concrete.
    # Kimi is a strong model with a reputation for being cheap, and on Sift's
    # token shape it is not: K3's $15.00/M output is 3x Haiku's, so it comes
    # out DEARER than the incumbent despite a cheaper cache-hit input rate.
    # Cache-miss input is used here, since roster narrowing already put every
    # prompt below the size where caching engages.
    Candidate("Kimi K3 (Moonshot)", 3.00, 15.00, False,
              source="platform.kimi.ai/docs/pricing/chat-k3"),
    # The cheapest general-purpose Kimi with a published price. Still only 10%
    # under Haiku on output, which is the axis that decides this pipeline.
    Candidate("Kimi K2.6 (Together)", 1.20, 4.50, True,
              source="together.ai/pricing"),
]


async def fetch(conn, days: int) -> list[dict]:
    rows = await conn.fetch(
        """
        SELECT operation,
               model,
               SUM(call_count)         AS calls,
               SUM(input_tokens)       AS input_tokens,
               SUM(output_tokens)      AS output_tokens,
               SUM(cache_read_tokens)  AS cache_read,
               SUM(cache_write_tokens) AS cache_write,
               SUM(estimated_cost_usd) AS usd
        FROM ai_usage_daily
        WHERE usage_date >= (CURRENT_DATE - ($1::int - 1))
          AND provider = 'anthropic'
        GROUP BY operation, model
        HAVING SUM(call_count) > 0
        ORDER BY SUM(estimated_cost_usd) DESC
        """,
        days,
    )
    return [dict(r) for r in rows]


async def articles_per_day(conn, days: int) -> float:
    val = await conn.fetchval(
        """
        SELECT count(*)::float / GREATEST(count(DISTINCT created_at::date), 1)
        FROM articles
        WHERE created_at >= (CURRENT_DATE - ($1::int - 1))
        """,
        days,
    )
    return float(val or 0.0)


def project(
    row: dict, cand: Candidate, batch_multiplier: float, uses_batch: bool
) -> float:
    """USD for this row's exact token volume, at the candidate's prices.

    `uses_batch` gates the discount on whether the STAGE goes through the Batch
    API, not on whether the model has one. Only context/primer/entity_extractor
    do; summarizer, the linker, the synthesizer and the confirmer are realtime.
    Applying the multiplier to all of them halved the incumbent's projection on
    four of seven stages — visible as projected $0.291 for summarizer against a
    measured $0.609 — which would have made every candidate look better than it
    is, in the direction that gets one shipped.
    """
    tokens_in = int(row["input_tokens"] or 0)
    tokens_out = int(row["output_tokens"] or 0)
    # Cache reads/writes are priced as plain input on a candidate: prompt
    # caching semantics differ per provider and most OpenAI-compatible hosts
    # report nothing, so assuming the discount carries over would understate.
    tokens_in += int(row["cache_read"] or 0) + int(row["cache_write"] or 0)

    cost = tokens_in * cand.input_per_m / 1e6 + tokens_out * cand.output_per_m / 1e6
    if uses_batch and cand.has_batch_discount:
        cost *= batch_multiplier
    return cost


def main_report(rows: list[dict], arts_per_day: float, days: int) -> dict:
    if not rows:
        print(
            "\nNo Anthropic rows with call_count > 0 in this window.\n"
            "If the pipeline has been running, the ledger write path is broken."
        )
        return {}

    total_in = sum(int(r["input_tokens"] or 0) for r in rows)
    total_out = sum(int(r["output_tokens"] or 0) for r in rows)

    print(f"\nwindow: {days} day(s)   ~{arts_per_day:,.0f} articles/day")
    print()

    if total_in == 0 and total_out == 0:
        print(
            "  TOKENS ARE ALL ZERO.\n\n"
            "  Rows exist and carry dollars, so the ledger is being written —\n"
            "  but migrations/029's token columns are not being populated. That\n"
            "  means log_usage/log_batch_usage are not passing them through, and\n"
            "  every projection below would be arithmetic on nothing.\n\n"
            "  Do not read the rest of this report until that is fixed."
        )
        return {"tokens_present": False}

    # ── measured shape ───────────────────────────────────────
    print("measured token shape per operation")
    print(
        f"  {'operation':32s} {'model':26s} {'calls':>7s} {'in/call':>9s} "
        f"{'out/call':>9s} {'out:in':>7s} {'$/1k art':>9s}"
    )
    per_1k = {}
    for r in rows:
        calls = int(r["calls"] or 0) or 1
        tin, tout = int(r["input_tokens"] or 0), int(r["output_tokens"] or 0)
        ratio = (tout / tin) if tin else float("inf")
        k = (r["usd"] or 0) / max(arts_per_day * days, 1) * 1000
        per_1k[(r["operation"], r["model"])] = k
        print(
            f"  {r['operation']:32s} {r['model']:26s} {calls:7d} "
            f"{tin/calls:9,.0f} {tout/calls:9,.0f} {ratio:7.2f} {k:9.3f}"
        )

    overall_ratio = total_out / total_in if total_in else 0.0
    print(f"\n  pipeline-wide out:in = {overall_ratio:.2f}")
    print(
        "  Output is priced at 5x input on Haiku, so a stage's out:in ratio\n"
        "  decides how much a candidate's output price matters to it."
    )

    # ── projection ───────────────────────────────────────────
    print("\n\nprojected $/1k articles by candidate")
    header = f"  {'operation':32s}"
    for c in CANDIDATES:
        header += f" {c.label[:22]:>23s}"
    print(header)

    out: dict = {"tokens_present": True, "operations": {}}
    totals = {c.label: 0.0 for c in CANDIDATES}
    for r in rows:
        spec = spec_for_wire_model(r["model"])
        inc_mult = spec.batch_price_multiplier if spec else 1.0
        needs_batch = BATCH in CAPABILITIES.get(r["operation"], frozenset())

        line = f"  {r['operation']:32s}"
        row_out = {}
        for c in CANDIDATES:
            usd = project(r, c, inc_mult, uses_batch=needs_batch)
            k = usd / max(arts_per_day * days, 1) * 1000
            totals[c.label] += k
            row_out[c.label] = round(k, 4)
            flag = "!" if (needs_batch and not c.has_batch_discount) else " "
            line += f" {k:22.3f}{flag}"
        print(line)
        out["operations"][f"{r['operation']}@{r['model']}"] = row_out

    print("  " + "-" * (32 + 24 * len(CANDIDATES)))
    line = f"  {'TOTAL $/1k articles':32s}"
    for c in CANDIDATES:
        line += f" {totals[c.label]:22.3f} "
    print(line)
    out["totals_per_1k"] = {k: round(v, 4) for k, v in totals.items()}

    print(
        "\n  ! = this stage runs through the Batch API and the candidate has no\n"
        "      confirmed batch discount, so it is priced at full rate here.\n"
        "      Separately, and true of EVERY non-Anthropic candidate: those\n"
        "      three stages would need their async-completion path rewritten,\n"
        "      because no other provider offers `messages.batches`. That is an\n"
        "      engineering cost this table does not price."
    )
    print(
        f"\n  Prices retrieved {PRICES_RETRIEVED} from each vendor's own pricing\n"
        "  page (see Candidate.source). They move — re-check before quoting.\n"
        "  COST ONLY: this says nothing about output quality, latency, JSON\n"
        "  reliability, or rate limits, and a cheaper model that fails index\n"
        "  alignment more often costs more in retries than it saves per token."
    )
    return out


async def main(days: int, out_json: str | None) -> int:
    url = os.environ.get("DATABASE_URL")
    if not url:
        print("DATABASE_URL is not set", file=sys.stderr)
        return 2
    conn = await asyncpg.connect(
        url, ssl="require" if "neon.tech" in url else None
    )
    try:
        rows = await fetch(conn, days)
        arts = await articles_per_day(conn, days)
    finally:
        await conn.close()

    report = main_report(rows, arts, days)
    if out_json and report:
        with open(out_json, "w") as f:
            json.dump(report, f, indent=2)
        print(f"\n  wrote {out_json}")
    if report.get("tokens_present") is False:
        return 1
    return 0


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--days", type=int, default=1)
    p.add_argument("--json", dest="out_json")
    a = p.parse_args()
    sys.exit(asyncio.run(main(a.days, a.out_json)))
