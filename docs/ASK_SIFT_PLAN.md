# Ask Sift — Agentic Chat Feature Plan

Purpose: ship an open-ended chat surface where users ask Sift questions in
natural language and an agent uses Sift's tools to answer. Turns "read
curated stories" into "ask the news." **The differentiator that justifies
native mobile over a PWA.**

**Status: approved for v1.5** (2026-05-20). Ships in BOTH web and Android v1
— not as a v1.1 add-on. Companion: lens-driven Refined Compare agent (see
`docs/REFINED_COMPARE_PLAN.md`) — same architecture, narrower system prompt,
structured rather than conversational output.

Related docs:
- `docs/MERGE_MCP_INTO_API.md` (consolidates the agent's tool layer)
- `docs/MOBILE_PROTOCOL_DECISION.md` (REST for app, MCP internal)
- `docs/REFINED_COMPARE_PLAN.md` (sibling specialized agent)
- `sift/docs/ANDROID_APP_v1.md` (Android v1 includes this feature in scope)

## Why this feature

Sift today is passive: users encounter what the pipeline chose. An agentic
chat adds an active mode where any question the data can answer becomes
accessible without users learning the navigation:

- "Compare how outlets covered the most recent FERC ruling"
- "Who funds Senator X's campaign?"
- "What happened in energy policy this week?"
- "What does ProPublica have on this bill?"

Same value Claude Desktop users get today via `sift-mcp`, brought into the
first-party web and mobile app surfaces.

## Why this is the mobile differentiator

Without Ask Sift, Android v1 is "reader + share extension" — a polished news
reader competing with Apple News, Artifact, Google News. With Ask Sift,
mobile becomes a civic-literacy agent that lives on the phone. That's the
wedge that justifies the 10-week native build over a PWA.

## Architecture

```
[Web chat UI at /ask]   [Android Compose chat UI]
        │                          │
        │ POST /api/ask  { messages, session_id? }
        ↓                          ↓
[sift-api: agent endpoint — same for both clients]
   ├─ Claude Sonnet 4.6 (Anthropic Messages API, tools enabled)
   ├─ Tools = 5 sift-mcp handlers (post-#62 merge: shared with the
   │   Refined Compare agent and the public MCP transport)
   ├─ Cost cap enforcement (per-turn, per-user-day, global)
   └─ SSE stream back to caller
        │ text/event-stream (browser EventSource / OkHttp EventSource)
        ↓
[Streaming chat UI with tool-call visibility + citations]
```

Two protocol layers (per `docs/MOBILE_PROTOCOL_DECISION.md`):
- App ↔ sift-api: REST POST + SSE (same shape as `/api/news/topic`)
- sift-api ↔ Claude's tool layer: either direct Python function calls
  (Pattern X) OR internal MCP client → in-process MCP server (Pattern Y).
  Implementation detail; both work. With Refined Compare as a sibling
  agent, Pattern Y becomes more attractive (one tool registry, multiple
  internal LLM clients).

User-facing protocol is REST. MCP stays internal plumbing.

## Specialized agents share the tool surface

Ask Sift is one of two specialized agent endpoints in sift-api:

| Endpoint | Purpose | Output shape | System prompt |
|---|---|---|---|
| `POST /api/ask` | Open-ended Q&A | Conversational text + citations | Open-ended; uses any of 5 tools |
| `POST /api/compare` (with `lens`) | Lens-driven outlet comparison | Structured `{outlets: [{outlet, framing}]}` | Constrained to compare through the user-supplied lens |

Both share: the same 5 handlers (post-merge), cost cap primitives,
Anthropic SDK invocation pattern, SSE streaming. They differ in: system
prompt, output schema, tool-call patterns the agent prefers.

This is exactly the case where Pattern Y (unified MCP, see
`docs/MERGE_MCP_INTO_API.md`) starts paying off — one tool registry
consumed by multiple internal LLM clients in the same server.

## v0 scope (ships in v1.5)

In:
- **Web chat UI** at `/ask`
- **Android chat UI** (Compose) in the same Android v1 release
- 5 tools available to agent: `search_articles`, `get_article`,
  `get_dossier`, `search_dossiers`, `compare_outlets`
- Streaming SSE with inline tool-call visibility
- Citation rendering: every article reference becomes a tappable link
- Auth: Clerk-signed-in for sustained use; anonymous trial with
  aggressive per-IP rate limit
- Cost caps: per-turn $0.50 hard, per-user-day $2 anon / $5 signed,
  global daily ceiling $50 (alarm at $30)
- Telemetry: per-conversation token + dollar cost, tool-call counts
- Kill-switch env var `ASK_SIFT_DISABLED=true` (disables both `/api/ask`
  AND the Refined Compare path)
- `/methodology` page updated to describe agent mode + grounding policy

Out (v1.6+):
- Conversation persistence across sessions
- Multi-turn memory / personalization
- Voice input
- Custom system prompts / personas
- Export / share conversation
- Public agent API for external callers (separate decision)

## SSE event shapes

```
event: text         data: { "delta": "..." }
event: tool_use     data: { "name": "...", "input": {...} }
event: tool_result  data: { "name": "...", "ok": true, "summary": "..." }
event: done         data: { "cost_usd": 0.08, "tokens_in": 1850, "tokens_out": 412 }
event: error        data: { "code": "rate_limited"|"cap_exceeded"|"upstream_error", "message": "..." }
```

## Cost model

Claude Sonnet 4.6 pricing as of plan date (verify before launch):
- Input: ~$3 per million tokens
- Output: ~$15 per million tokens

Typical turn:
- Input: system + tool schemas + history + tool results = 2k-6k tokens
- Output: 500-1500 tokens
- 1-3 tool calls per turn; each adds 500-2000 input tokens for results
- Per-turn realistic: $0.02-0.10
- Per-conversation realistic: $0.05-0.30

Shared budget pool with Refined Compare (Ask Sift conversations + Refined
Compare calls draw from the same per-user-day envelope).

Cost guardrails:
- Per-turn cap (hard): $0.50
- Per-user-day cap: $2 anonymous, $5 authenticated (shared with Refined Compare)
- Global daily ceiling: $50/day with PostHog alarm at $30 (also shared)
- Kill-switch: `ASK_SIFT_DISABLED=true` disables both endpoints

## UX must-haves

- **Tool-call visibility**: "Searching articles…" / "Looking up FERC…"
  inline so users see the agent is grounded
- **Citations**: every factual claim from a tool result must link to the
  article that backed it. Uncited entity claims should not exist
- **Stop generation**: client-side abort cancels the upstream Claude call
- **Error states**: cap-hit → "try again later"; rate-limit → retry-after;
  upstream failure → "Sift's brain is offline — try a curated story"
- **Empty / first-run state**: 3 example prompts showing the surface area
- **Streaming progress**: tokens appear within ~2s of submit

## Hallucination risk + mitigations

Civic content + LLM = highest-stakes hallucination domain. One bad fact
about a real politician kills credibility.

1. System prompt mandates every factual claim must come from a tool
   result, not training data
2. Output filter: strips uncited claims about real-world politicians /
   orgs / bills, replaces with "I couldn't verify this in Sift's index"
3. Citation density target: ≥ 1 citation per real-entity claim
4. Eval set: 50 representative prompts; weekly regression on citation
   rate + factual accuracy (manual rubric)
5. Public-facing methodology paragraph explains the grounding policy

## Roadmap fit

Dependencies:
- `sift-mcp` #2 (cost caps) — mandatory; rolled into #62 Phase 1
- Merge `sift-mcp` into `sift-api` (#62) — strongly recommended
- `sift-api` #54 (DMCA + methodology) — soft prereq for public launch

Sequencing (finalized 2026-05-20):

**Ships in v1.5, web AND Android.** Backend agent endpoint lands during
Android build weeks 6-8 (parallel to mobile work). Android Compose chat
UI is part of the v1 build (~3-5 extra days of mobile work on top of
reader + share extension).

Tier `v1.5` · effort `weeks` (web backend + Android UI together).

## Open questions

1. Pattern X vs Y for the internal tool layer — implementation detail
   inside the merge; either works. Pattern Y likely preferred because of
   the sibling Refined Compare agent
2. Model: Sonnet 4.6 for v0; consider Haiku once eval set establishes
   acceptable quality floor
3. Tool subset: start with all 5
4. Public API for `/api/ask`: app-only for v0
5. Conversation persistence: out of v0; `session_id` reserved for v1.6+

## Acceptance criteria (v0)

- [ ] `/ask` route on web renders a chat UI
- [ ] Android Compose chat UI matches web UX semantics
- [ ] SSE consumer in OkHttp EventSource validated on Android
- [ ] Backend agent endpoint streams SSE responses
- [ ] All 5 tools wired and demonstrably called for representative prompts
- [ ] Citations rendered for every tool-derived claim
- [ ] Cost telemetry: per-conversation total + global daily visible
- [ ] All 3 cap tiers verified (per-turn, per-user-day, global)
- [ ] Shared cap pool with Refined Compare verified
- [ ] Eval set of 50 prompts passes weekly accuracy check (≥ 90% citation
      rate, no uncited entity claims in spot check)
- [ ] Kill-switch env var disables both `/api/ask` and Refined Compare
- [ ] README + `/methodology` page updated to describe agent mode

## Risks

- Hallucination on civic content → mitigations above; eval-driven
- Cost surprise from abusive users → caps + email gate + IP RL
- Latency complaints (5-30s per turn) → progress UI + tool-call
  visibility helps; consider model-mode toggle if needed
- Reviewer pushback ("but Sift was a calm reading app") → chat is
  secondary; `/news` stays the homepage; chat is opt-in via clear CTA
- Brand risk if grounding fails → strict citation policy + eval +
  manual review of public-facing examples before launch
- App Store / Play Store review of AI chat apps → Play Store has been
  more permissive than App Review on AI summarizers in 2026, but not
  zero-friction; reviewer notes should explain the grounding policy
