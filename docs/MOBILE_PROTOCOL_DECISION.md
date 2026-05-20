# Mobile App → Protocol Decision Checklist

Purpose: resolve whether the mobile app needs to call sift-mcp (MCP
protocol) or sift-api (REST) before committing to sift-mcp #4
(HTTP/SSE + hosting). Wrong answer here costs 1–2 weeks of misplaced
infrastructure work.

## Inputs to read first

- docs/IOS_APP_PLAN.md
- docs/IOS_APP_ASSESSMENT.md
- docs/IOS_VS_ANDROID.md
- Any mobile-app PRD or wireframes (where do they live?)

If those don't exist or don't specify AI architecture: stop and write
that decision down before any backend work begins. The mobile app's
AI architecture is a precondition for v0.5 backend work, not an
output of it.

## Decision tree

For EACH AI/data feature the mobile app will have, answer:

### Q1 — Where does the LLM run for this feature?

- [ ] **No LLM at tap time** — feature just displays AI content that
      was generated server-side ahead of time (e.g., article primers,
      pre-computed dossiers, daily briefings).
      → **REST**. No MCP needed. Skip the rest.

- [ ] **On-device LLM** (Apple Intelligence, on-device Claude SDK, etc.).
      → Go to Q2.

- [ ] **Server-side LLM** — your backend calls Claude on the user's
      behalf and returns the result.
      → Go to Q3.

- [ ] **External AI** (Siri, ChatGPT app, third-party agent invokes
      Sift via App Intents / Siri Shortcuts).
      → **App Intents / Siri Intents** on iOS. Not MCP, not REST.

### Q2 — On-device LLM: does it need tool use?

- [ ] **Yes, it makes tool calls** (e.g., "Get me the dossier for
      Senator X" mid-conversation).
      → MCP is plausible, but the native Anthropic SDK with a custom
      tool-handler that hits your REST API is usually simpler than
      embedding an MCP client on mobile. Pick MCP only if the
      on-device runtime expects MCP natively.

- [ ] **No, it just generates text from context the app provides.**
      → **REST**. App fetches data, hands the text to the LLM. MCP
      adds nothing.

### Q3 — Server-side LLM: hosted MCP or in-process?

- [ ] **Server runs Claude with tool use against our data.**
      → **Internal MCP** (in-process or stdio invocation from within
      sift-api). NOT a public hosted MCP. sift-mcp #4 is not required.

- [ ] **Users authenticate with their own Anthropic key and call
      Claude directly from the app, which then calls our MCP.**
      → Unusual; almost always **REST** instead. The user-pays model
      is hard to scale and creates support overhead.

## Per-feature worksheet

| Feature | What user taps | Required data | Where AI runs | Protocol |
|---|---|---|---|---|
| Daily briefing | "Brief me" tile | Today's top 10 + dossier links | Server-side at tap | REST → server uses internal MCP |
| Story compare | "Compare" on article | claims per outlet | Server-side at tap | REST → server uses internal MCP |
| Topic search | Search bar | Vector-ranked articles | None (pre-indexed) | REST |
| Ask-Sift chat | "Ask" overlay | Open-ended | On-device LLM with tools | MCP (only if SDK demands) or REST |
| Siri "what's new in energy" | Siri activation | Top 3 stories | None | App Intents |

Fill the table in for the actual planned features. If column 5 has
zero "MCP" entries, the right move is:

- Drop sift-mcp #4 entirely from the v0.5 roadmap.
- Ship sift-mcp #2 (cost caps) standalone, keep sift-mcp stdio-only.
- Build the mobile REST surface on sift-api instead.

If column 5 has any "MCP (internal)" entries, the right move is:

- Drop sift-mcp #4 (no public hosting needed).
- Merge sift-mcp into sift-api so internal calls don't cross process
  boundaries (see docs/MERGE_MCP_INTO_API.md).

If column 5 has any "MCP (public)" entries (rare), proceed with #4
but only for those specific features and only after confirming the
mobile platform actually wants the MCP transport.

## Stop conditions

Do NOT proceed to sift-mcp #4 implementation if any of these are true:

1. The mobile app's AI architecture is not yet documented.
2. The per-feature worksheet above has zero "MCP (public)" entries.
3. sift-api #54 (DMCA audit + methodology) has not landed.
4. No specific mobile feature has named a launch date that requires
   the hosted MCP to be live first.

## Owner

- Mobile lead answers Q1–Q3 per feature.
- Backend lead reviews and decides architecture.
- Decision recorded in docs/IOS_APP_PLAN.md or a new
  docs/MOBILE_PROTOCOL_DECISION_OUTCOME.md.
