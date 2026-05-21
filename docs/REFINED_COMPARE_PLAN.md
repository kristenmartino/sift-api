# Refined Compare — Lens-Driven Comparison Agent

Purpose: extend `POST /api/compare` to accept a user-supplied **lens** that
drives an agent loop to surface outlet-specific framing on a specific axis
(definition, stance, framing, decision, character, etc.) rather than the
current "all claims, grouped by agreement" output.

**Status: approved for v1.5** (2026-05-20). Sibling specialized agent to
Ask Sift (see `docs/ASK_SIFT_PLAN.md`); same architecture, different system
prompt, structured output instead of conversational.

Related docs:
- `docs/ASK_SIFT_PLAN.md` (sibling agent; shared cap pool, shared backbone)
- `docs/MERGE_MCP_INTO_API.md` (shared tool layer)
- `sift/docs/ANDROID_APP_v1.md` (Android v1 includes a Compare button with `Focus on...` input)

## Why this exists

Today's `compare_outlets` returns "show me everything outlets said, tag
each claim as unanimous/disputed/unique." That's a flat-list answer to a
flat-list question.

Real comparison questions have specific axes:
- "Compare how outlets DEFINE Order 1920's impact on grid reliability"
- "How does each outlet EXPLAIN the senator's vote against the bill?"
- "How do they describe the candidate's CHARACTER?"
- "Compare their FRAMING of the strike's economic impact"

Today's compare can't answer those — the lens is implicit (agreement
across claims) rather than user-specified. Users have to mentally filter
claims to find the lens-relevant ones.

Refined Compare lets the user specify the lens; the agent surfaces just
the lens-relevant framing per outlet.

## Behavior summary

```
POST /api/compare
{
  "topic": "FERC Order 1920",
  "lens": "how outlets define the impact on grid reliability",
  "outlets": ["reuters", "ap", "wsj"]    // optional
}
```

- **No `lens`** → routes to existing deterministic path (current
  `compare_outlets` behavior, claim-extraction grouped by agreement)
- **With `lens`** → routes to agent loop (Refined Compare)

Same endpoint, same response envelope. The lens parameter just changes
the backend path. Frontend renders both as outlet-by-outlet cards;
deterministic fills `outlets[].framing` mechanically from claims, agent
fills it with lens-targeted synthesis.

## Architecture

Identical to Ask Sift (see `docs/ASK_SIFT_PLAN.md`):
- Server-side agent loop in sift-api
- Anthropic Messages API with tools enabled
- Tools = 5 sift-mcp handlers (post-merge)
- Cost cap primitives shared with Ask Sift
- SSE stream back to client (clients render outlet cards incrementally as
  the agent finishes each)

The shared tool surface across Ask Sift + Refined Compare is where Pattern
Y (unified MCP) earns its keep — same tool registry, multiple internal LLM
clients with different system prompts.

## System prompt (the actual artifact)

```
You are a comparative news analyst. The user gives you a TOPIC and a LENS
(a specific question or angle for comparison). Your job is to compare how
outlets cover the topic THROUGH the lens — not to surface every claim.

Available tools:
- search_articles(query, category?, limit?)
- get_article(article_id)
- compare_outlets(topic, outlets?, article_limit?, web_fallback?)
  ← use this for raw material; then refine through the lens
- get_dossier(entity_type, slug)
- search_dossiers(entity_type, query, limit?)

Workflow:
1. Parse the lens. Identify what specific axis the user is asking about
   (definition, stance, framing, decision, character, etc.).
2. Call compare_outlets(topic) to get raw article + claim material.
3. Evaluate which articles + claims actually address the lens. Discard
   the rest.
4. For each outlet with lens-relevant coverage, extract their specific
   framing in 2-4 sentences using neutral language.
5. If lens coverage is thin, do targeted re-searches with rephrased
   queries (search_articles with lens-specific keywords).
6. Synthesize: where do outlets diverge on the lens? Where do they
   converge? Which outlets don't address the lens at all?

Output must match the response schema exactly. Every framing claim must
cite at least one article_id. Never invent quotes. If an outlet didn't
cover the lens, list it under outlets_with_no_coverage_of_lens — don't
fabricate.

If the lens is too vague to act on (e.g., "compare them" with no axis),
return summary explaining the lens is underspecified and suggest 2-3
specific lens phrasings.

Constraints:
- Neutral framing language. Describe what outlets say; do not editorialize.
- At least one citation per framing claim.
- Lens-relevance ratio (framings addressing the lens / total framings) ≥ 0.9.
```

## Output schema

```json
{
  "topic": "FERC Order 1920",
  "lens": "how outlets define the impact on grid reliability",
  "outlets": [
    {
      "outlet": "Reuters",
      "framing": "Frames grid reliability as the order's primary justification, citing FERC commissioners' statements about avoiding future Texas-style failures.",
      "key_phrases": ["reliability mandate", "regional planning"],
      "article_ids": ["a_123", "a_456"]
    }
  ],
  "summary": "Outlets diverge primarily on whether grid reliability is framed as the chief justification (Reuters, AP, Bloomberg) or as one of several concerns where cost dominates (WSJ, Fox). All concede reliability benefits exist; the divergence is in editorial weighting.",
  "outlets_with_no_coverage_of_lens": ["Politico"],
  "lens_coverage_quality": "strong"
}
```

When called without a lens (deterministic path), the response uses the
same envelope but `lens` is null and `outlets[].framing` is synthesized
mechanically from extracted claims rather than via agent loop.

## Cost model

Per call:
- ~$0.15-0.40 (Sonnet, agent loop with 2-4 tool calls)
- vs ~$0.05 for deterministic compare (Haiku, one-shot)

Shared budget pool with Ask Sift:
- Per-user-day: $5 signed-in, $2 anon
- Global daily ceiling: $50/day with alarm at $30 (`ASK_SIFT_DAILY_USD_CAP`)
- Kill-switch: `ASK_SIFT_DISABLED=true` disables both endpoints

## Behavioral differences from deterministic compare

| Dimension | Deterministic (no lens) | Refined (with lens) |
|---|---|---|
| Input | `{topic}` | `{topic, lens}` |
| Output shape | `{outlets: [{outlet, framing}]}` with mechanically-extracted framings | Same envelope, lens-targeted framings |
| Lens-relevance | All claims | Only lens-relevant framings |
| Outlets surfaced | All with coverage | All with lens-relevant coverage; others in `outlets_with_no_coverage_of_lens` |
| Model | Haiku, one-shot | Sonnet, agent loop |
| Latency | 10-15s | 20-40s |
| Cost / call | ~$0.05 | ~$0.15-0.40 |

## Why same endpoint, not separate

Frontend UX wants the same outlet-by-outlet card view either way. Same
data shape, just different richness. Splitting into two endpoints would
fork the frontend rendering for no benefit. A `lens` parameter is the
right router.

Backend routes internally based on `lens` presence; clients don't have to
know which path ran (though `lens_coverage_quality` and other agent-
specific fields are populated only on the agent path).

## v0 scope (ships in v1.5)

In:
- `lens` parameter on existing `POST /api/compare` endpoint
- Agent loop using the system prompt above
- Strict Pydantic validation on output schema; agent retries up to 2x if
  output is invalid
- Cost cap integration with Ask Sift's budget pool
- Telemetry: per-call cost, tool-call count, lens-coverage-quality
- Kill-switch shared with Ask Sift
- Web Compare button gains a "Focus on…" text input (when filled, sends
  `lens` parameter)
- Android Compare button gains the same input

Out (v1.6+):
- Multi-lens compare (lens is single-string for v0)
- Streaming partial results as each outlet completes (returns all at
  once; if needed, add SSE per-outlet later)
- Lens templates / saved-lens shortcuts

## Hallucination risk

Same domain as Ask Sift but narrower output surface. Specific mitigations:
- Output filter: `outlets[].framing` must have ≥ 1 `article_id`; reject
  framings without citations
- Eval set: 30 lens+topic pairs with human-rated framings; check lens-
  relevance ratio weekly
- Schema enforcement: Pydantic validation; agent retries up to 2x if
  output is invalid

## Eval criteria (v0 ship)

- [ ] Lens-relevance ratio ≥ 0.9 on eval set
- [ ] Every framing claim has ≥ 1 article_id citation
- [ ] No fabricated outlet entries (all outlets in response must have
      actual lens-relevant articles in the index)
- [ ] When lens is underspecified, agent returns a clear "lens too broad"
      message rather than hallucinating
- [ ] Latency p95 ≤ 40s
- [ ] Cost p95 ≤ $0.30 per call

## Roadmap fit

Ships alongside Ask Sift in v1.5. Recommended order:

1. Land `sift-api` #62 Phase 0 (merge source — handlers now live in sift-api)
2. Build Ask Sift agent loop backbone (#63 Phase 1)
3. Add Refined Compare agent loop reusing the same backbone
4. Web frontend: Ask Sift `/ask` UI + Compare button `Focus on…` input
5. Android frontend: chat UI + Compare button `Focus on…` input

Tier `v1.5` · effort `weeks` (overlaps with Ask Sift implementation).

## Tracking

Tracked as a phase of `sift-api` #63 (Ask Sift v0), since v0 implementations
share so much they can ship as one work stream. If they diverge in scope,
split into its own issue at that point.
