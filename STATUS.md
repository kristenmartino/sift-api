# sift-api — STATUS

**Updated:** 2026-05-20
**Tier:** v1.5 (civic-literacy pivot backend) → v1.6 (Ask Sift agent loop, planned)
**Velocity:** High (10+ PRs / week)

## Active focus

Civic-literacy pivot backend. Recently shipped: fixed entity linker (LLM-gated, A/B-able) in `services/entity_linker_llm.py`; 170 new dossier entries via 4 new CSVs + seed scripts in `data/` and `scripts/`; `coverage_audit.py` archived measuring post-fix link rate.

**Merge in flight (2026-05-20):** `sift-mcp` consolidating into this repo as a second transport. Architecture in [`docs/MERGE_MCP_INTO_API.md`](./docs/MERGE_MCP_INTO_API.md); tracked in [#62](https://github.com/kristenmartino/sift-api/issues/62). Ask Sift agent loop (#63) queued behind it as the first v1.6 feature.

## Open strategic questions

Three live unknowns (one resolved 2026-05-20 — moved to Recent decisions). None block current work; all shape decisions in the next 1–3 months.

### 1. When does sift-api need to scale beyond Railway hobby tier?

Pipeline runs every 10 min, ingests ~135 sources, calls Claude for summaries + entity linking + primer generation. Today: comfortably under hobby-tier limits.

Watch for:
- Pipeline run time exceeds 8 min (close to the 10-min cadence)
- Anthropic monthly bill from pipeline crosses $50/mo (today: ~$15)
- Neon Postgres connection pool `max=5` starts queuing requests visibly
- Native app launches and pushes write volume up
- **Added 2026-05-20:** Ask Sift v0 (tier-v1.6) starts incurring per-turn Claude spend; watch `ASK_SIFT_DAILY_USD_CAP` headroom alongside pipeline spend

### 2. Is the LLM-based entity linker durable, or does it need a v2?

Phase 3.G.2 shipped the LLM linker with disambiguation rules added since. It's working but it's a moving target — every dossier expansion changes its catalog, and the prompt keeps needing tweaks.

What would resolve this: a stable eval set with target precision/recall numbers, run on every PR that touches `services/entity_linker_llm.py`. Until that exists, the linker stays in "iterate fast" mode.

### 3. DMCA fair-use posture for AI summarization

Per Railway's 2026 fair-use clause (lists "Hosting/Distribution of DMCA protected content" as prohibited) + the live NYT / Perplexity / AP litigation landscape: audit needed to confirm `services/` doesn't write original article body HTML or images to disk on Railway, and `/methodology` (sibling `sift` repo) needs a transformative-use posture paragraph before any user-submitted-URL features (e.g. iOS share extension) ship. Tracked as Next-3 item #2 below.

## Next 3

1. **[committed]** Dossier expansion — completion ([#53](https://github.com/kristenmartino/sift-api/issues/53)). Close out the 170-entry seed; coverage targets per category; promote entity linker out of A/B once link-rate hits target. Tier `v1.5` · `effort-week`.
2. **[committed]** DMCA audit + methodology update ([#54](https://github.com/kristenmartino/sift-api/issues/54)). Verify `services/` doesn't persist source HTML/images on Railway disk or in logs; add transformative-use paragraph + symmetric-application note to `/methodology` (sibling `sift` repo); pre-draft DMCA counter-notice template. Tier `v1.5` · `effort-day`.
3. **[committed]** Merge `sift-mcp` into `sift-api` ([#62](https://github.com/kristenmartino/sift-api/issues/62)). Consolidate as one Python service exposing two transports (REST + MCP, stdio + optional HTTP/SSE). Rolls in `sift-mcp` #2 cost caps (Phase 1) and likely supersedes `sift-mcp` #4 (Phase 2). Architecture spec in [`docs/MERGE_MCP_INTO_API.md`](./docs/MERGE_MCP_INTO_API.md). Tier `v1.5` · `effort-week`.

Queued behind Next-3:
- **Ask Sift v0 — agentic chat (web)** ([#63](https://github.com/kristenmartino/sift-api/issues/63)). Tier `v1.6` · `effort-weeks`. Recommended slot: parallel to Android v1 build weeks 6–8. Feature plan in [`docs/ASK_SIFT_PLAN.md`](./docs/ASK_SIFT_PLAN.md).

## Blocked-on

- Native platform direction (resolved 2026-05-20 — Android-first per `sift/docs/ANDROID_APP_v1.md`)
- Triage of #62 (merge) and #63 (Ask Sift) into the sequenced roadmap with civic-literacy v1.5 work

## Recent decisions

- **2026-05-20** — **`sift-mcp` merges into `sift-api`.** Architecture spec in [`docs/MERGE_MCP_INTO_API.md`](./docs/MERGE_MCP_INTO_API.md). Tracked in [#62](https://github.com/kristenmartino/sift-api/issues/62) with 4 phases. Resolves the long-standing "merge?" open strategic question. Drivers: existing duplication between sift-api `/analyze/compare` and sift-mcp `compare_outlets` is real (per `sift/app/api/compare/route.ts:55`); merge consolidates handlers and unblocks the Ask Sift agent loop.
- **2026-05-20** — **Mobile is REST-only.** Per `sift/docs/ANDROID_APP_v1.md` (active Android v1 plan, Path A from `IOS_VS_ANDROID.md`). `sift-mcp` #4 (hosted HTTP/SSE) deferred indefinitely — no mobile demand. Rationale in [`docs/MOBILE_PROTOCOL_DECISION.md`](./docs/MOBILE_PROTOCOL_DECISION.md).
- **2026-05-20** — **Ask Sift v0 planned as `tier-v1.6`** (web-only; mobile inherits in Android v1.1). Open-ended chat surface with the 5 sift-mcp tools wired into an internal agent loop on sift-api. Plan in [`docs/ASK_SIFT_PLAN.md`](./docs/ASK_SIFT_PLAN.md). Tracked in [#63](https://github.com/kristenmartino/sift-api/issues/63).
- **~2026-05** — **Entity linker LLM-gated, A/B-able rollout** (`services/entity_linker_llm.py`). Lets dossier link-rate be measured pre/post fix without blast-radius risk.
- **~2026-05** — **Hybrid index + web search architecture** (sift-mcp). Chose B+C smart fallback over pure-index or pure-web for the comparison tool. Pattern likely applies to the compare workflow in this repo too.
- **~2026-05** — **26-outlet pool with smart DB-exclusion selection** (sift-mcp). Replaced 4-outlet fixed default.
- **~2026-05** — **`compare_outlets` returns unified claims array with source tag** (sift-mcp). Simpler client-side rendering than separate-sections shape.
- **~2026-05** — **`load_dotenv(override=True)` for predictable env precedence** (sift-mcp). Broader pattern for the family.
- **2026-05-20** — Canonical `/v1/*` API for mobile **deferred**. See `sift/docs/IOS_APP_ASSESSMENT.md`. Reuse Next.js routes for now.

*Dates marked `~2026-05` are approximate — fill in if known.*

---

*See also: [`CLAUDE.md`](./CLAUDE.md) (orientation), [`BACKLOG.md`](./BACKLOG.md) (deferred items), [`README.md`](./README.md), [`init.sql`](./init.sql). Sibling repos: `sift` (frontend, owns user-facing reads + civic surface), `sift-mcp` (MCP server — merging into this repo per #62).*
