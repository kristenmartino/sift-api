# sift-api — STATUS

**Updated:** 2026-05-20
**Tier:** v1.5 (civic-literacy pivot backend)
**Velocity:** High (10+ PRs / week)

## Active focus

Civic-literacy pivot backend. Recently shipped: fixed entity linker (LLM-gated, A/B-able) in `services/entity_linker_llm.py`; 170 new dossier entries via 4 new CSVs + seed scripts in `data/` and `scripts/`; `coverage_audit.py` archived measuring post-fix link rate.

## Open strategic questions

Four live unknowns. None block current work; all shape decisions in the next 1–3 months.

### 1. When does sift-api need to scale beyond Railway hobby tier?

Pipeline runs every 10 min, ingests ~135 sources, calls Claude for summaries + entity linking + primer generation. Today: comfortably under hobby-tier limits.

Watch for:
- Pipeline run time exceeds 8 min (close to the 10-min cadence)
- Anthropic monthly bill from pipeline crosses $50/mo (today: ~$15)
- Neon Postgres connection pool `max=5` starts queuing requests visibly
- Native app launches and pushes write volume up

### 2. Is the LLM-based entity linker durable, or does it need a v2?

Phase 3.G.2 shipped the LLM linker with disambiguation rules added since. It's working but it's a moving target — every dossier expansion changes its catalog, and the prompt keeps needing tweaks.

What would resolve this: a stable eval set with target precision/recall numbers, run on every PR that touches `services/entity_linker_llm.py`. Until that exists, the linker stays in "iterate fast" mode.

### 3. Does `sift-mcp` eventually merge into `sift-api` as one service with two surfaces?

The MCP is a separate Python service today that shares Postgres but nothing else. Merging would let `compare_outlets`-style hybrid workflows live next to the rest of the LangGraph pipeline.

Won't resolve until ~3 months of usage data + an actual feature that needs cross-surface code-sharing.

### 4. DMCA fair-use posture for AI summarization

Per Railway's 2026 fair-use clause (lists "Hosting/Distribution of DMCA protected content" as prohibited) + the live NYT / Perplexity / AP litigation landscape: audit needed to confirm `services/` doesn't write original article body HTML or images to disk on Railway, and `/methodology` (sibling `sift` repo) needs a transformative-use posture paragraph before any user-submitted-URL features (e.g. iOS share extension) ship. Tracked as Next-3 item #2 below.

## Next 3

1. **[committed]** Dossier expansion — completion (see Issues tab). Close out the 170-entry seed; coverage targets per category; promote entity linker out of A/B once link-rate hits target. Tier `v1.5` · `effort-week`.
2. **[committed]** DMCA audit + methodology update (see Issues tab). Verify `services/` doesn't persist source HTML/images on Railway disk or in logs; add transformative-use paragraph + symmetric-application note to `/methodology` (sibling `sift` repo); pre-draft DMCA counter-notice template. Tier `v1.5` · `effort-day`.
3. **[sketch]** `/v1/*` mobile API endpoints — deferred until native platform direction is settled. See `sift/docs/IOS_APP_ASSESSMENT.md` for why this is not the right time. Tier `v2` · `effort-weeks`.

## Blocked-on

- Native platform direction (mobile API surface depends on which client comes first — currently leaning Android-first per `sift/docs/IOS_VS_ANDROID.md`)

## Recent decisions

- **~2026-05** — **Entity linker LLM-gated, A/B-able rollout** (`services/entity_linker_llm.py`). Lets dossier link-rate be measured pre/post fix without blast-radius risk.
- **~2026-05** — **Hybrid index + web search architecture** (sift-mcp). Chose B+C smart fallback over pure-index or pure-web for the comparison tool. Pattern likely applies to the compare workflow in this repo too.
- **~2026-05** — **26-outlet pool with smart DB-exclusion selection** (sift-mcp). Replaced 4-outlet fixed default.
- **~2026-05** — **`compare_outlets` returns unified claims array with source tag** (sift-mcp). Simpler client-side rendering than separate-sections shape.
- **~2026-05** — **`load_dotenv(override=True)` for predictable env precedence** (sift-mcp). Broader pattern for the family.
- **2026-05-20** — Canonical `/v1/*` API for mobile **deferred**. See `sift/docs/IOS_APP_ASSESSMENT.md`. Reuse Next.js routes for now.

*Dates marked `~2026-05` are approximate — fill in if known.*

---

*See also: [`CLAUDE.md`](./CLAUDE.md) (orientation), [`BACKLOG.md`](./BACKLOG.md) (deferred items), [`README.md`](./README.md), [`init.sql`](./init.sql). Sibling repos: `sift` (frontend, owns user-facing reads + civic surface), `sift-mcp` (MCP server, separate cadence).*
