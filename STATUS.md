# sift-api — STATUS

**Updated:** 2026-06-04
**Tier:** v1.5 (civic-literacy pivot backend)
**Velocity:** High (10+ PRs / week)

## Active focus

Civic-literacy pivot backend, with two threads surfacing this week (2026-06): **content quality** — a generation quality gate for `whyItMatters` / `contextPrimer` (rubric + LLM-judge eval, #90 — **shipped 2026-06-04**, see Recent decisions) after a 500-article audit found those AI lines inconsistent (restatement + editorializing); and **outlet-data integrity** — prod `outlet_profiles` cleanup shipped (#91, 77→72), with the seed-CSV↔prod divergence + an authoritative seeder queued (#93). Also open: topic search → sift-api (#79/#80), a NULL-embedding repair pass (#76), and an outlet ingestion-status field (#73). Recently shipped: entity linker fix (LLM-gated, A/B-able) in `services/entity_linker_llm.py`; 170 new dossier entries via seed scripts in `data/` + `scripts/`; `coverage_audit.py` archived measuring post-fix link rate.

## Open strategic questions

Four live unknowns (one — #3 — now resolved below). None block current work; all shape decisions in the next 1–3 months.

### 1. When does sift-api need to scale beyond Railway hobby tier?

Pipeline runs every 30 min, ingests ~135 sources, calls Claude for summaries + entity linking + primer generation. Today: comfortably under hobby-tier limits.

Watch for:
- Pipeline run time approaches the 30-min cadence (e.g. exceeds ~25 min)
- Anthropic monthly bill from pipeline crosses $50/mo (today: ~$15)
- Neon Postgres connection pool `max=5` starts queuing requests visibly
- Native app launches and pushes write volume up

### 2. Is the LLM-based entity linker durable, or does it need a v2?

Phase 3.G.2 shipped the LLM linker with disambiguation rules added since. It's working but it's a moving target — every dossier expansion changes its catalog, and the prompt keeps needing tweaks.

What would resolve this: a stable eval set with target precision/recall numbers, run on every PR that touches `services/entity_linker_llm.py`. Until that exists, the linker stays in "iterate fast" mode.

### 3. Does `sift-mcp` eventually merge into `sift-api` as one service with two surfaces?

The MCP is a separate Python service today that shares Postgres but nothing else. Merging would let `compare_outlets`-style hybrid workflows live next to the rest of the LangGraph pipeline.

**Resolved (2026-05-20) → merge.** `compare_outlets`-style hybrid logic moves next to the LangGraph pipeline; one service, two transports (REST + MCP) with shared handlers. Canonical record: [`sift/docs/DECISIONS.md` D41](https://github.com/kristenmartino/sift/blob/main/docs/DECISIONS.md); tracked at #62 (Phase 0 pending).

### 4. DMCA fair-use posture for AI summarization

Per Railway's 2026 fair-use clause (lists "Hosting/Distribution of DMCA protected content" as prohibited) + the live NYT / Perplexity / AP litigation landscape: audit needed to confirm `services/` doesn't write original article body HTML or images to disk on Railway, and `/methodology` (sibling `sift` repo) needs a transformative-use posture paragraph before any user-submitted-URL features (e.g. iOS share extension) ship. Tracked as Next-3 item #2 below.

## Next 3

1. **[committed]** Dossier expansion — completion (see Issues tab). Close out the 170-entry seed; coverage targets per category; promote entity linker out of A/B once link-rate hits target. Tier `v1.5` · `effort-week`.
2. **[committed]** DMCA audit + methodology update (see Issues tab). Verify `services/` doesn't persist source HTML/images on Railway disk or in logs; add transformative-use paragraph + symmetric-application note to `/methodology` (sibling `sift` repo); pre-draft DMCA counter-notice template. Tier `v1.5` · `effort-day`.
3. **[sketch]** `/v1/*` mobile API endpoints — deferred until native platform direction is settled. See `sift/docs/IOS_APP_ASSESSMENT.md` for why this is not the right time. Tier `v2` · `effort-weeks`.

*Newly surfaced this week: content-quality gate (#90) — **shipped** (see Recent decisions 2026-06-04); outlet-data integrity (#93 — the authoritative seeder), which is the foundation for sift's deliberate source expansion (kristenmartino/sift#151).*

## Blocked-on

- Native platform direction (mobile API surface depends on which client comes first — currently leaning Android-first per `sift/docs/IOS_VS_ANDROID.md`)

## Recent decisions

- **2026-06-04** — **`why_it_matters` / `context_primer` quality gate shipped (#90).** The sift#150 audit found `why_it_matters` either restated the summary or hand-waved with clichés; lexical overlap is too weak to gate it (misses paraphrased restatement AND editorial fluff), so the fix is **semantic, at generation time** — not the rejected frontend overlap-suppressor. Three layers: (1) a rewritten generation rubric demanding a concrete *verifiable* stake, strictly neutral, **null-over-filler** (`why_it_matters` is now NULL by design for low-stake articles; the card hides it); (2) a free deterministic cliché/restatement backstop (`services/quality_gate.py`) in the batch-result write path — clichés, not lexical overlap, do the work (both live failures, cop-fired + Kepner, sit *above* any safe lexical threshold and are caught only by cliché detection); (3) an **offline** Sonnet LLM-judge (`services/judge.py`) + eval harness (`scripts/eval_why_it_matters.py`). `context_primer.background` got the lighter touch (cliché trim only; glossary `terms` always kept). **Stratified 500-article prod baseline (50/category), judge before→after:** cliché/editorial **77%→31%**, adds-verifiable-significance **9%→37%**, PASS (all three axes) **5%→35%**; the rubric suppresses **46%** as no-real-stake. Judge stays out of the hot path (zero added prod cost); a flag-gated runtime judge is the documented follow-up for the residual paraphrase-restatement (~38% of shown lines). Existing prod lines clear via `scripts/regate_existing.py` (`ON CONFLICT` doesn't regenerate). **#71 signal:** suppression concentrates in sports/entertainment (54–58%) vs energy/health (30–38%) — direct data for "do sports & entertainment belong in news-with-footnotes." **→ implements [`sift/docs/DECISIONS.md` D38](https://github.com/kristenmartino/sift/blob/main/docs/DECISIONS.md).**
- **2026-06-03** — **Outlet table cleanup (#91): pruned 5 drifted rows from prod `outlet_profiles`** (77 → 72) via idempotent, transactional `scripts/dedupe_outlet_profiles.py`. Removed two duplicate profiles (`bbc` → canonical `bbc-news`, `bloomberg-news` → `bloomberg`; 3 `bbc` entity_links repointed) and the three excluded Yahoo verticals (`yahoo-news`/`-finance`/`-sports`, which contradicted `/methodology`'s aggregator exclusion). Surfaced by the sift #153 dynamic-outlet-count work. **Process finding → [#93](https://github.com/kristenmartino/sift-api/issues/93):** prod has ~15 legit outlets NOT in the seed CSV, and `seed_outlet_profiles.py` is upsert-only (never prunes), so the CSV is no longer prod's source of truth. **→ canonical decision: [`sift/docs/DECISIONS.md` D40](https://github.com/kristenmartino/sift/blob/main/docs/DECISIONS.md).**
- **~2026-05** — **Entity linker LLM-gated, A/B-able rollout** (`services/entity_linker_llm.py`). Lets dossier link-rate be measured pre/post fix without blast-radius risk.
- **~2026-05** — *sift-mcp architecture decisions* (hybrid index + web fallback; 26-outlet smart-exclusion pool; `compare_outlets` unified-claims-array shape; `load_dotenv(override=True)`) live in **sift-mcp**'s own STATUS — fold into this repo when #62 merges sift-mcp in. Not re-stated here to avoid drift.
- **2026-05-20** — **Native + agentic architecture calls** (cross-repo) → [`sift/docs/DECISIONS.md`](https://github.com/kristenmartino/sift/blob/main/docs/DECISIONS.md): `sift-mcp` merges into `sift-api` (**D41**, resolves Open-Q #3; tracked #62); mobile is **REST-only** (**D42**); **Refined Compare** (`lens`) + **Ask Sift** in v1.5, web + Android (**D43**; tracked #63).
- **2026-05-20** — Canonical `/v1/*` mobile API **deferred** → canonical record in [`sift/docs/DECISIONS.md`](https://github.com/kristenmartino/sift/blob/main/docs/DECISIONS.md) **D33** (reuse Next.js routes for now).

*Dates marked `~2026-05` are approximate — fill in if known.*

---

*See also: [`CLAUDE.md`](./CLAUDE.md) (orientation), [`BACKLOG.md`](./BACKLOG.md) (deferred items), [`README.md`](./README.md), [`init.sql`](./init.sql), and [`sift/docs/DECISIONS.md`](https://github.com/kristenmartino/sift/blob/main/docs/DECISIONS.md) — the **canonical cross-repo decision register** (record shared architecture decisions there, not duplicated across STATUS files). Sibling repos: `sift` (frontend, owns user-facing reads + civic surface), `sift-mcp` (MCP server, separate cadence).*
