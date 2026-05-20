# sift-api — status

> **Pre-session ritual:** `cat STATUS.md && gh pr list && gh issue list && cat BACKLOG.md`. See [CLAUDE.md](CLAUDE.md).

## Active focus

**Civic-literacy MVP Phase 3.G** — dossier expansion + entity-linking quality. Recently shipped: federal agency dossiers (15 added in #49), search instrumentation table (#52), primer panel-expand telemetry. In flight in the working tree: judge profiles, foreign politicians, executive politicians, agency budget enrichment from OMB, supplemental outlet profiles. Pairs with the Sift frontend's civic-literacy pivot.

## Open strategic questions

Three live unknowns. None block current work; all shape decisions in the next 1–3 months.

### 1. When does sift-api need to scale beyond Railway hobby tier?

Pipeline runs every 10 min, ingests ~135 sources, calls Claude for summaries + entity linking + primer generation. Today: comfortably under hobby-tier limits.

Watch for:
- Pipeline run time exceeds 8 min (close to the 10-min cadence)
- Anthropic monthly bill from pipeline crosses $50/mo (today: ~$15)
- Neon Postgres connection pool max=5 starts queuing requests visibly
- Mobile app launches and pushes write volume up

### 2. Is the LLM-based entity linker durable, or does it need a v2?

Phase 3.G.2 shipped the LLM linker (#44) with disambiguation rules added since (#46, #48). It's working but it's a moving target — every dossier expansion changes its catalog, and the prompt keeps needing tweaks.

What would resolve this: a stable eval set with target precision/recall numbers, run on every PR that touches `services/entity_linker_llm.py`. Until that exists, the linker stays in "iterate fast" mode.

### 3. Does `sift-mcp` eventually merge into `sift-api` as one service with two surfaces?

Mirror of strategic question #2 in [sift-mcp STATUS.md](https://github.com/kristenmartino/sift-mcp/blob/main/STATUS.md). The MCP is a separate Python service today that shares Postgres but nothing else. Merging would let `compare_outlets`-style hybrid workflows live next to the rest of the LangGraph pipeline.

Won't resolve until ~3 months of usage data + an actual feature that needs cross-surface code-sharing.

## Next 3

Issues live in GitHub; this is the human-readable summary.

1. **[#53 Dossier expansion — completion](https://github.com/kristenmartino/sift-api/issues/53)** *(tier-v1.5, effort-week)*. Federal agencies done; judges, foreign politicians, executive politicians, supplemental outlets in working tree. Land the remaining categories + backfill `entity_links` for affected articles.
2. **[#54 DMCA audit + methodology update](https://github.com/kristenmartino/sift-api/issues/54)** *(tier-v1.5, effort-day)*. Audit the existing scraping/storage posture against current DMCA + fair-use, update methodology page on the frontend.
3. **[#16 Track-A follow-up: deferred query rewrite, pool bump, statement_timeout](https://github.com/kristenmartino/sift-api/issues/16)** *(no labels yet, effort-day)*. Revisit if `feed-perf` job warns. Today not blocking but flagged for when it does.

## Blocked-on

Nothing engineering-blocked. Scale work is gated on demand signal, not capacity (see strategic question #1).

## Recent decisions

- **LLM-based entity linker with disambiguation** (#44). Chose Claude Haiku 4.5 over the prior regex approach for the entity linking step in the pipeline. Trades determinism for catalog flexibility — the catalog now expands per-dossier-add without prompt rewrites in most cases. Subsequent fixes (#46 reject state/party-only matches, #48 tighten term-pick bar) refined the prompt against observed false-positive patterns.
- **Federal agency dossiers as their own type** (#49, with `OrgType.agency` added in `sift#91`). Agencies don't fit the existing org taxonomy (think tanks, advocacy, corporations) — they're a distinct civic entity worth structured representation. 15 added covering executive branch + independent regulators.
- **Right-leaning RSS feed addition** (#47). Corpus audit found 0 right/lean-right articles; added 8 feeds to balance the spectrum. Symmetric-application rule applies — methodology page documents the corpus curation policy.
- **Search query instrumentation table** (#52). Topic search funnel — what users actually search for — now logged. Drives future relevance tuning and informs which civic terms need dossier coverage.

## Where things live

### Code

- `app/main.py`, `app/routers/` — FastAPI surface (pipeline trigger, compare workflow)
- `workflows/` — LangGraph workflows (pipeline_workflow, compare_workflow, story_workflow)
- `services/` — pipeline node implementations (entity_extractor, entity_linker, entity_linker_llm, primer_generator, story_clusterer, etc.)
- `migrations/` + `init.sql` + `app/db.py:_apply_migrations` — schema. See [CLAUDE.md](CLAUDE.md#schema) for the dual-file pattern.
- `scripts/explain_feed_queries.py` — feed-perf diagnostic; also in CI

### Planning + state

- **STATUS.md** (this file) — top-of-mind: active focus, open questions, Next 3, blockers, recent decisions
- **BACKLOG.md** — everything deferred, in prose: stretch items, bugs/quirks to revisit. Promote to issues when committed.
- **GitHub issues** — formally tracked work. See [`gh issue list`](https://github.com/kristenmartino/sift-api/issues).
- **GitHub Project** ([Sift](https://github.com/users/kristenmartino/projects/3)) — board spanning sift, sift-api, sift-mcp.

If you can't find something: `gh issue list` → `cat BACKLOG.md` → `git log --oneline` → ask. The pre-session ritual hits all four.
