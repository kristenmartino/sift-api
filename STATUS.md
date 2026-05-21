# sift-api — STATUS

**Updated:** 2026-05-20
**Tier:** v1.5 (civic-literacy pivot backend + merge + agentic surfaces)
**Velocity:** High (10+ PRs / week)

## Active focus

Civic-literacy pivot backend. Recently shipped: fixed entity linker (LLM-gated, A/B-able) in `services/entity_linker_llm.py`; 170 new dossier entries via 4 new CSVs + seed scripts in `data/` and `scripts/`; `coverage_audit.py` archived measuring post-fix link rate.

**Three pieces of v1.5 in flight (all decided 2026-05-20):**
1. **Merge `sift-mcp` into `sift-api`** — architecture in `docs/MERGE_MCP_INTO_API.md`; tracked in [#62](https://github.com/kristenmartino/sift-api/issues/62). Consolidates `compare_outlets` duplication, unblocks the agentic surfaces below.
2. **Ask Sift agent loop** (#63) — open-ended chat surface; ships in BOTH web and Android v1 (not v1.1). Plan in `docs/ASK_SIFT_PLAN.md`. **This is the differentiator that justifies native mobile.**
3. **Refined Compare agent endpoint** — sibling specialized agent to Ask Sift; lens-driven structured comparison. Plan in `docs/REFINED_COMPARE_PLAN.md`. Tracked as a phase of #63.

## Open strategic questions

Two live unknowns (two resolved 2026-05-20 — moved to Recent decisions). None block current work.

### 1. When does sift-api need to scale beyond Railway hobby tier?

Pipeline runs every 10 min, ingests ~135 sources, calls Claude for summaries + entity linking + primer generation. Today: comfortably under hobby-tier limits.

Watch for:
- Pipeline run time exceeds 8 min (close to the 10-min cadence)
- Anthropic monthly bill from pipeline crosses $50/mo (today: ~$15)
- Neon Postgres connection pool `max=5` starts queuing requests visibly
- Native app launches and pushes write volume up
- **Ask Sift + Refined Compare** start incurring per-turn Claude spend; watch `ASK_SIFT_DAILY_USD_CAP` headroom alongside pipeline spend

### 2. Is the LLM-based entity linker durable, or does it need a v2?

Phase 3.G.2 shipped the LLM linker with disambiguation rules added since. It's working but it's a moving target — every dossier expansion changes its catalog, and the prompt keeps needing tweaks.

What would resolve this: a stable eval set with target precision/recall numbers, run on every PR that touches `services/entity_linker_llm.py`. Until that exists, the linker stays in "iterate fast" mode.

## Next 3 (now Next 4 with the agentic work)

1. **[committed]** Dossier expansion — completion ([#53](https://github.com/kristenmartino/sift-api/issues/53)). Close out the 170-entry seed; coverage targets per category; promote entity linker out of A/B once link-rate hits target. Tier `v1.5` · `effort-week`.
2. **[committed]** DMCA audit + methodology update ([#54](https://github.com/kristenmartino/sift-api/issues/54)). Verify `services/` doesn't persist source HTML/images on Railway disk or in logs; add transformative-use paragraph + symmetric-application note to `/methodology` (sibling `sift` repo); pre-draft DMCA counter-notice template. Tier `v1.5` · `effort-day`.
3. **[committed]** Merge `sift-mcp` into `sift-api` ([#62](https://github.com/kristenmartino/sift-api/issues/62)). Consolidate as one Python service exposing two transports (REST + MCP). Rolls in `sift-mcp` #2 cost caps (Phase 1) and likely supersedes `sift-mcp` #4 (Phase 2). Architecture in `docs/MERGE_MCP_INTO_API.md`. Tier `v1.5` · `effort-week`.
4. **[committed]** Ask Sift + Refined Compare agent endpoints ([#63](https://github.com/kristenmartino/sift-api/issues/63)). Web `/ask` + Android Compose chat UI for open-ended chat; `lens` parameter on `/api/compare` for lens-driven structured comparison. Plans in `docs/ASK_SIFT_PLAN.md` and `docs/REFINED_COMPARE_PLAN.md`. Tier `v1.5` · `effort-weeks` (overlaps with Android v1 build weeks 6-8).

## Blocked-on

- Native platform direction (resolved 2026-05-20 — Android-first per `sift/docs/ANDROID_APP_v1.md`)
- Mobile protocol decision (resolved 2026-05-20 — REST-only per `docs/MOBILE_PROTOCOL_DECISION.md`)
- Triage of #62 (merge) and #63 (Ask Sift + Refined Compare) into a sequenced roadmap with civic-literacy work

## Recent decisions

- **2026-05-20** — **Ask Sift retiered to `v1.5`** (was queued as `v1.6`). Ships in Android v1 alongside web, not as a v1.1 add-on. Reasoning: Ask Sift IS the differentiator that justifies native mobile vs. PWA; deferring removes the wedge.
- **2026-05-20** — **Refined Compare agent endpoint added** as sibling to Ask Sift. Same architecture, narrower system prompt, structured output. Lens-driven path on existing `/api/compare` endpoint. Plan in `docs/REFINED_COMPARE_PLAN.md`.
- **2026-05-20** — **Multiple-internal-LLM-clients pattern adopted.** Ask Sift + Refined Compare = two agent loops sharing the 5-tool surface. This is the case where Pattern Y (unified MCP — see `docs/MERGE_MCP_INTO_API.md`) becomes the cleaner choice over Pattern X. Implementation detail; both work.
- **2026-05-20** — **`sift-mcp` merges into `sift-api`.** Architecture spec in `docs/MERGE_MCP_INTO_API.md`. Tracked in [#62](https://github.com/kristenmartino/sift-api/issues/62) with 4 phases. Resolves the long-standing "merge?" open strategic question. Drivers: existing duplication between sift-api `/analyze/compare` and sift-mcp `compare_outlets` (per `sift/app/api/compare/route.ts:55`); merge consolidates handlers and unblocks the agentic surfaces.
- **2026-05-20** — **Mobile is REST-only.** Per `sift/docs/ANDROID_APP_v1.md`. Even agentic features (Ask Sift, Refined Compare) use REST/SSE — the agent loop runs server-side; MCP is internal plumbing if used at all. `sift-mcp` #4 (hosted HTTP/SSE) deferred. Rationale in `docs/MOBILE_PROTOCOL_DECISION.md`.
- **~2026-05** — **Entity linker LLM-gated, A/B-able rollout** (`services/entity_linker_llm.py`). Lets dossier link-rate be measured pre/post fix without blast-radius risk.
- **~2026-05** — **Hybrid index + web search architecture** (sift-mcp). Chose B+C smart fallback over pure-index or pure-web for the comparison tool. Pattern applies to the compare workflow in this repo too (post-merge).
- **~2026-05** — **26-outlet pool with smart DB-exclusion selection** (sift-mcp). Replaced 4-outlet fixed default.
- **~2026-05** — **`compare_outlets` returns unified claims array with source tag** (sift-mcp). Simpler client-side rendering than separate-sections shape.
- **~2026-05** — **`load_dotenv(override=True)` for predictable env precedence** (sift-mcp). Broader pattern for the family.
- **2026-05-20** — Canonical `/v1/*` API for mobile **deferred**. See `sift/docs/IOS_APP_ASSESSMENT.md`. Reuse Next.js routes for now; net-new `/api/ask` + `/api/compare` (with lens) endpoints land alongside the merge.

*Dates marked `~2026-05` are approximate — fill in if known.*

---

*See also: [`CLAUDE.md`](./CLAUDE.md) (orientation), [`BACKLOG.md`](./BACKLOG.md) (deferred items), [`README.md`](./README.md), [`init.sql`](./init.sql). Sibling repos: `sift` (frontend, owns user-facing reads + civic surface), `sift-mcp` (MCP server — merging into this repo per #62).*
