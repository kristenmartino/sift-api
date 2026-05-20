# sift-api + sift-mcp — Merged Service Architecture

Purpose: spec what it looks like to consolidate sift-mcp into sift-api
as a single Python service exposing two transports (HTTP REST + MCP).
Resolves STATUS.md "open strategic question #2" in favor of merging.

## Why now

- v0.5 (sift-mcp #2 + #4) would otherwise duplicate cost-cap
  primitives, auth middleware, and observability across two services.
- The mobile-app trigger forces a decision on hosting; merging
  collapses the deploy/DNS/observability work for #4 into a no-op
  (it's just a sift-api deploy).
- Three months of usage data threshold from STATUS.md is moot if
  v0.5 itself doubles the duplication tax.

## Current state

```
sift-api/                          sift-mcp/
  app/                               src/sift_mcp/
    pipeline/  (RSS, summarize)        server.py  (5 MCP tools)
    routes/    (REST endpoints)        db.py      (asyncpg pool)
    db.py      (asyncpg pool)        Dockerfile
  Dockerfile                         (Railway: separate service, not
  (Railway: api.siftnews...)          yet deployed)
```

Both pools hit the same Neon Postgres. Both consume the same env vars
(DATABASE_URL, VOYAGE_API_KEY, ANTHROPIC_API_KEY). compare_outlets
duplicates ranking/embedding logic that arguably should live next to
the pipeline that produced the index.

## Target state

Single repo (sift-api), single service, two transports backed by
shared handlers:

```
sift-api/
  app/
    main.py                  # FastAPI app; mounts both transports
    transports/
      http.py                # REST routes (existing)
      mcp.py                 # MCP tool registry (HTTP/SSE + stdio)
    handlers/
      articles.py            # search_articles, get_article
      dossiers.py            # get_dossier, search_dossiers
      compare.py             # compare_outlets (index + web)
    pipeline/                # RSS, summarize, dedupe, embed (unchanged)
    db.py                    # one asyncpg pool, shared
    auth.py                  # Bearer middleware (mounted on /mcp)
    caps.py                  # cost cap primitives (sift-mcp #2)
    entrypoints/
      web.py                 # uvicorn launch — production
      mcp_stdio.py           # stdio launch — local Claude Desktop/Code
  Dockerfile
  railway.toml
```

Tool handlers are transport-agnostic:

```python
# app/handlers/articles.py
async def search_articles(query: str, category: str | None, limit: int):
    ...

# app/transports/http.py
@router.get("/api/search")
async def http_search(q: str, category: str | None = None, limit: int = 10):
    return await handlers.search_articles(q, category, limit)

# app/transports/mcp.py
@mcp.tool()
async def search_articles(query: str, category: str | None = None, limit: int = 10):
    return await handlers.search_articles(query, category, limit)
```

## Surfaces after merge

| Surface | URL / invocation | Auth | Notes |
|---|---|---|---|
| REST (sift frontend, mobile app) | api.siftnews.kristenmartino.ai/api/* | session / token | existing |
| MCP HTTP/SSE | api.siftnews.kristenmartino.ai/mcp | Bearer | sift-mcp #4's hosting becomes a route mount |
| MCP stdio (local) | uv run python -m sift_api.entrypoints.mcp_stdio | none | for Claude Desktop / Code; replaces sift-mcp invocation |

`mcp.siftnews.kristenmartino.ai` subdomain becomes unnecessary.

## Migration plan

1. **Phase 0 (do BEFORE #2 and #4):** Merge sift-mcp source into
   sift-api on a feature branch.
   - Use `git subtree add` (or merge with --allow-unrelated-histories)
     to preserve sift-mcp's git history inside sift-api.
   - Wire mcp.py transport mount into FastAPI.
   - Add entrypoints/mcp_stdio.py.
   - Confirm all 5 tools work via both transports (smoke-test against
     MCP Inspector for stdio; curl /mcp for HTTP/SSE).
   - Land in one PR.

2. **Phase 1 (sift-mcp #2):** Build cost-cap primitives in
   app/caps.py. Both transports automatically benefit.

3. **Phase 2 (sift-mcp #4):** Add Bearer auth middleware to the /mcp
   mount only. Tokens table migration in sift-api/init.sql. No new
   Railway service, no new subdomain, no new env-var set, no DNS work.

4. **Cleanup:** Archive sift-mcp repo with a README pointing at
   sift-api. Don't delete — preserves issue history (#2–#8 still
   discoverable). Existing Claude Desktop / Code configs need a
   one-line update to the new entrypoint; document in sift-api README.

## v0.5 effort delta

| Work | Two-service estimate | Merged estimate |
|---|---|---|
| #2 cost caps | 1 week | 4–5 days (no cross-repo coordination) |
| #4 HTTP/SSE + hosting | 1 week | 2–3 days (route mount, not new service) |
| Phase 0 merge | n/a | 1–2 days |
| **Total** | **~2 weeks** | **~7–10 days** |

Net: ~3–5 days saved, plus the ongoing duplication-tax savings on
every future change that touches data access.

## Trade-offs

**Pro**
- One asyncpg pool, one logging surface, one deploy target.
- Tool handlers and pipeline business logic share modules — change
  ranking once, MCP search reflects it.
- Cost caps and auth live in one place, applied uniformly.
- Smaller blast radius for ops mistakes (one service to monitor).
- Honest answer to STATUS.md question #2 instead of letting it drift.

**Con**
- MCP and HTTP can't scale independently. Theoretical at current
  traffic; reconsider if MCP traffic ever spikes beyond what shared
  capacity can absorb.
- sift-api becomes a bigger codebase. Mitigated by the handlers/
  transports/ split, which keeps each transport thin.
- Git history fragmentation if subtree merge isn't done carefully.
- Loses the "sift-mcp as a standalone open-source MCP demo" framing.
  Re-export as a sample if that framing matters for portfolio /
  hiring.

**Reversible?** Mostly yes. The handler module split means re-extracting
sift-mcp later is a `git filter-branch` away. The harder-to-reverse
piece is any custom MCP framework integration done at the FastAPI
mount point — keep that thin.

## Open questions to resolve before Phase 0

1. **MCP SDK + FastAPI compatibility.** Confirm the mcp Python SDK
   can mount as an ASGI sub-app inside FastAPI for the HTTP/SSE
   transport. Expected yes; verify in a 30-min spike.
2. **Pipeline + tool handler separation.** Confirm no circular
   imports between pipeline/ (writes data) and handlers/ (reads
   data). They share db.py but should not import each other.
3. **Stdio entrypoint ergonomics.** Confirm `uv run python -m
   sift_api.entrypoints.mcp_stdio` boots cleanly without spinning up
   FastAPI/uvicorn for local Claude Desktop / Code use.
4. **CI matrix.** Existing sift-api CI runs pipeline + REST tests.
   Add MCP tool tests; cap CI runtime at current budget.

## Decision needed

If the answer is "merge," reorder the v0.5 roadmap:

1. Phase 0 (1–2 days): merge sift-mcp into sift-api.
2. sift-api #54 (1 day): DMCA audit + methodology page.
3. sift-mcp #2 → sift-api caps (4–5 days): cost caps as merged code.
4. Mobile-app discovery checklist resolves protocol question.
5. If #4 still needed: ship as /mcp route + Bearer auth (2–3 days).

If the answer is "don't merge," go with the original two-week
two-PR plan and accept the duplication tax.
