# Ask Sift — Agentic Chat Feature Plan

Purpose: ship an open-ended chat surface where users ask Sift questions in
natural language and an agent uses Sift's tools to answer. Turns "read
curated stories" into "ask the news." Strongest civic-literacy
differentiator on the roadmap that isn't already shipping.

Status: open product decision. This doc defines what v0 would look like
if greenlit; whether to ship and when is undecided.

Related docs:
- docs/MERGE_MCP_INTO_API.md (consolidates the agent's tool layer)
- docs/MOBILE_PROTOCOL_DECISION.md (clarifies REST-for-app, MCP-internal)
- sift/docs/ANDROID_APP_v1.md (mobile lands later than web for chat)

## Why this feature

Sift today is passive: users encounter what the pipeline chose. An
agentic chat adds an active mode where any question the data can answer
becomes accessible without users learning the navigation:

- "Compare how outlets covered the most recent FERC ruling"
- "Who funds Senator X's campaign?"
- "What happened in energy policy this week?"
- "What does ProPublica have on this bill?"

Same value Claude Desktop users get today via sift-mcp, brought into the
first-party app surface where most users actually live.

## Architecture

```
[Web chat UI / mobile chat UI (v1.1+)]
       │ POST /api/ask  { messages, session_id? }
       ↓
[sift-api: agent endpoint]
   ├─ Claude Sonnet 4.6 (Anthropic Messages API, tools enabled)
   ├─ Tools wired to existing 5 sift-mcp handlers (post-merge:
   │   shared; pre-merge: duplicated)
   ├─ Cost cap enforcement (per-turn, per-user-day, global)
   └─ SSE stream back to caller
       │ text/event-stream
       ↓
[App: streaming chat UI with tool-call visibility + citations]
```

Two protocol layers (per docs/MOBILE_PROTOCOL_DECISION.md framing):
- App ↔ sift-api: REST POST + SSE (same shape as /api/news/topic SSE)
- sift-api ↔ Claude's tool layer: direct Python function calls
  (post-merge) or internal MCP client (pre-merge)

User-facing protocol is REST. MCP is internal plumbing, never exposed
publicly to the chat surface (separate from sift-mcp #4 question).

## v0 scope

In:
- Web chat UI at `/ask`
- 5 tools available to agent: `search_articles`, `get_article`,
  `get_dossier`, `search_dossiers`, `compare_outlets`
- Streaming SSE with inline tool-call visibility
- Citation rendering: every article reference becomes a tappable link
  to the existing article-detail view
- Auth: Clerk-signed-in for sustained use; anonymous trial with
  aggressive per-IP rate limit
- Cost caps: per-turn $0.50 hard, per-user-day $2 anon / $5 signed,
  global daily ceiling $50 (alarm at $30)
- Telemetry: per-conversation token + dollar cost, tool-call counts
- Kill-switch env var `ASK_SIFT_DISABLED=true`
- `/methodology` page updated to describe agent mode + grounding policy

Out (v1.1+):
- Conversation persistence across sessions
- Multi-turn memory / personalization
- Voice input
- Mobile chat UI (waits for web validation)
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

Cost guardrails:
- Per-turn cap (hard): $0.50 — defense against runaway agent loops
- Per-user-day cap: $2 anonymous, $5 authenticated
- Global daily ceiling: $50/day with PostHog alarm at $30
- Kill-switch: `ASK_SIFT_DISABLED=true` disables `/api/ask` cleanly

## UX must-haves

- **Tool-call visibility**: "Searching articles…" / "Looking up FERC…"
  inline so users see the agent is grounded, not making things up
- **Citations**: every factual claim that came from a tool result must
  link to the article that backed it. Sentences without citations
  should not contain claims about real-world entities
- **Stop generation**: client-side abort cancels the upstream Claude call
- **Error states**: cap-hit → "try again later"; rate-limit → retry-after;
  upstream failure → "Sift's brain is offline — try a curated story"
- **Empty / first-run state**: 3 example prompts that demonstrate the
  surface area
- **Streaming progress**: tokens appear within ~2s of submit, never appear
  to hang

## Hallucination risk + mitigations

Civic content + LLM = highest-stakes hallucination domain. Sift's brand
is accuracy; one bad fact about a real politician kills credibility.

Mitigations:
1. System prompt mandates every factual claim must come from a tool
   result, not training data. No "I think X is on the Energy Committee"
   — only "according to [get_dossier result]…"
2. Output filter: strips uncited claims about real-world politicians /
   orgs / bills, replaces with "I couldn't verify this in Sift's index"
3. Citation density target: ≥ 1 citation per real-entity claim
4. Eval set: 50 representative prompts; weekly regression on citation
   rate + factual accuracy (manual rubric)
5. Public-facing methodology paragraph explains the grounding policy
   and what happens when the agent can't find an answer

## Roadmap fit

Dependencies:
- sift-mcp #2 (cost caps) — mandatory; chat amplifies cost exposure
- Merge sift-mcp into sift-api — strongly recommended; see
  `docs/MERGE_MCP_INTO_API.md`. Without merge, the agent loop either
  duplicates tool implementations or runs an internal MCP client
- sift-api #54 (DMCA + methodology) — soft prereq; methodology page
  should describe agent-mode grounding policy

Sequencing options:

**A. Ship as web v1.0 differentiator alongside Android launch**
- Pro: launches with the strongest possible product story
- Con: concurrent work; risk of Android delay
- Added effort: ~2 weeks

**B. Ship as web v1.1 after Android validates**
- Pro: cleaner sequencing; web telemetry before mobile
- Con: launches without the differentiator that justifies mobile
- Added effort: ~2 weeks after v1.0

Recommendation: **web-only v1.0** (no Android chat in v1, add in v1.1).
Web ships chat first; mobile inherits once it's proven.

## Open questions

1. Web-first vs both-at-launch — web-first recommended
2. Model: Sonnet 4.6 for v0; consider Haiku once eval set establishes
   acceptable quality floor for cost optimization
3. Tool subset: start with all 5, or restrict to `search_articles` +
   `get_dossier` for v0 to limit blast radius?
4. Public API for `/api/ask`: app-only for v0; external (with Bearer)
   could come later as a separate product decision
5. Conversation persistence: out of v0, but `session_id` field in the
   API leaves room for v1.1 add

## Acceptance criteria (v0)

- [ ] `/ask` route on web renders a chat UI
- [ ] Backend agent endpoint streams SSE responses
- [ ] All 5 tools wired and demonstrably called for representative prompts
- [ ] Citations rendered for every tool-derived claim
- [ ] Cost telemetry: per-conversation total + global daily visible
- [ ] All 3 cap tiers verified (per-turn, per-user-day, global)
- [ ] Eval set of 50 prompts passes weekly accuracy check (≥ 90%
      citation rate, no uncited entity claims in spot check)
- [ ] Kill-switch env var disables `/api/ask` cleanly
- [ ] README + `/methodology` page updated to describe agent mode

## Risks

- Hallucination on civic content → mitigations above; eval-driven
- Cost surprise from abusive users → caps + email gate + IP RL
- Latency complaints (5-30s per turn) → progress UI + tool-call
  visibility helps; consider model-mode toggle if dissatisfaction
  emerges in telemetry
- Reviewer pushback ("but Sift was a calm reading app") → chat is
  secondary; `/news` stays the homepage; chat is opt-in via clear CTA
- Brand risk if grounding fails → strict citation policy + eval +
  manual review of public-facing examples before launch
