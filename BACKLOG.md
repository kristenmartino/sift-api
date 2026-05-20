# sift-api — backlog

Items not committed to a current milestone. Promote to GitHub issues when work is committed; until then, capture here in prose so nothing gets lost.

> **What goes here:** half-formed ideas, deferred features that don't fit a current `tier-*` milestone, quirks worth tracking but not urgent. See [`CLAUDE.md`](CLAUDE.md) for the where-to-file-new-work decision tree.

## Stretch / nice-to-have

- *(Add items here as they surface. Prose, not bullet-perfect — the point is to not lose ideas, not to perfectly organize them.)*

## Bugs / quirks to revisit

- *(Same — add items as you hit them. Tie back to a commit or issue if you can.)*

## Considered and rejected

Architectural alternatives we discussed and chose against. Captured so we don't re-litigate; reasoning is easy to revisit if circumstances change.

- **Canonical `/v1/*` mobile API in sift-api** *(deferred, May 2026)* — proposed in the original iOS plan; the cross-functional assessment correctly pushed back that it's the right move at maturity, not pre-PMF. Reuse Next.js routes for now; collapse later when a second client validates the surface. See `sift/docs/DECISIONS.md#D33` and `sift/docs/IOS_APP_ASSESSMENT.md`.
