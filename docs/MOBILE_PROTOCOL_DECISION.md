# Mobile App → Protocol Decision Checklist

Purpose: resolve whether the mobile app needs to call sift-mcp (MCP
protocol) or sift-api (REST) before committing to sift-mcp #4
(HTTP/SSE + hosting). Wrong answer here costs 1–2 weeks of misplaced
infrastructure work.

**Decision landed 2026-05-20**: Android v1 is REST-only. Mobile does not
need hosted MCP. Even with agentic features (Ask Sift chat, Refined
Compare), the user-facing protocol stays REST/SSE — the agent loop runs
server-side and MCP, if used, is internal plumbing. See completed worksheet
below.

## Inputs to read first

- `sift/docs/ANDROID_APP_v1.md` (active v1 plan)
- `sift/docs/IOS_APP_PLAN.md` (under review)
- `sift/docs/IOS_APP_ASSESSMENT.md`
- `sift/docs/IOS_VS_ANDROID.md`

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
      embedding an MCP client on mobile.

- [ ] **No, it just generates text from context the app provides.**
      → **REST**. App fetches data, hands the text to the LLM. MCP
      adds nothing.

### Q3 — Server-side LLM: hosted MCP or in-process?

- [ ] **Server runs Claude with tool use against our data.**
      → **Internal MCP** or **direct Python function calls** — either
      works inside sift-api. The mobile app still talks REST/SSE to
      sift-api's agent endpoint. Public hosted MCP not required.

- [ ] **Users authenticate with their own Anthropic key and call Claude
      directly from the app, which then calls our MCP.**
      → Unusual; almost always **REST** instead. The user-pays model is
      hard to scale and creates support overhead.

## Per-feature worksheet — Android v1 (filled 2026-05-20)

| Feature | What user taps | Required data | Where AI runs | Protocol |
|---|---|---|---|---|
| Feed | Category tab | Pre-curated articles | None (pipeline ran ahead) | **REST** — existing `/api/news` |
| Article detail | Article card | Article + primer + entity links + outlet | None (pre-computed) | **REST** — Next.js route |
| Topic search | Search bar | Vector-ranked + `web_search` fallback | Server-side, streamed | **REST/SSE** — existing `/api/news/topic` |
| Bookmarks | Bookmark icon | List of user bookmarks | None | **REST** — existing `/api/bookmarks` |
| "Sift this URL" | Android share sheet → Sift | Fresh primer + entity links | Server-side at tap (Claude) | **REST** — new `/v1/share/sift-this` on sift-api |
| Push notifications | (passive delivery) | Article deep-link payload | None at delivery | **REST** (register) + **FCM** (delivery) |
| "Read at source" | Source button | Original article URL | None | **Chrome Custom Tabs** (system web) |
| Glossary chip | Tap chip | Term definition + dossier link | None (in primer payload already) | None at tap; dossier link via Custom Tabs |
| **Ask Sift chat** | "Ask" tab | Open-ended LLM response | **Server-side (agent loop)** | **REST/SSE** — new `/api/ask` on sift-api. Internal MCP or direct fn calls inside the agent loop — not visible to mobile |
| **Compare (deterministic)** | "Compare" button on article (no Focus input) | Outlet-by-outlet claim view | Server-side (Haiku one-shot) | **REST** — existing `/api/compare` |
| **Refined Compare** | "Compare" button + "Focus on…" text input | Outlet-by-outlet framing through user's lens | **Server-side (agent loop)** | **REST/SSE** — same `/api/compare` endpoint; presence of `lens` param routes to agent path |

**Column 5 has zero "MCP (public)" entries.** Even with two agentic
surfaces added (Ask Sift, Refined Compare), the user-facing protocol
remains REST/SSE for all features. The agent loops run server-side; MCP
is internal plumbing if used at all.

## What this triggers (per the checklist's own rules)

- Drop `sift-mcp` #4 from the v0.5 roadmap. (Done — see issue comment
  and `sift-api` #62 Phase 2 which supersedes it.)
- Ship `sift-mcp` #2 (cost caps) standalone, but absorb into `sift-api`
  #62 Phase 1 as part of the merge. (Done — see issue comment.)
- Build the mobile REST surface on `sift-api` instead of expanding
  `sift-mcp`. New mobile endpoints: `/v1/share/sift-this`,
  `/v1/devices/register`, `/api/ask`, `/api/compare` (with `lens`).

## Stop conditions (now resolved)

The original stop conditions for proceeding to `sift-mcp` #4 were:

1. ~~The mobile app's AI architecture is not yet documented.~~ →
   Documented in `ANDROID_APP_v1.md`.
2. ~~The per-feature worksheet above has zero "MCP (public)" entries.~~
   → Confirmed zero entries.
3. ~~`sift-api` #54 (DMCA audit + methodology) has not landed.~~ → In
   progress.
4. ~~No specific mobile feature has named a launch date that requires
   the hosted MCP to be live first.~~ → None do.

All resolved in the "don't ship" direction. `sift-mcp` #4 stays deferred
until non-mobile demand surfaces (external AI clients, third-party agent
integrations, etc.).

## Owner

- Mobile lead answered Q1-Q3 per feature 2026-05-20.
- Backend lead (Kristen) confirmed REST architecture.
- Decision recorded in `docs/ANDROID_APP_v1.md` and this doc.
