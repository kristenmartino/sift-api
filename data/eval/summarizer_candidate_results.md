# Summarizer candidate A/B — 2026-08-13

Corpus: `summarizer_corpus.jsonl`, 150 articles, 56 outlets, captured from a live
RSS fetch. Incumbent baseline: 5 repeats, category pairwise agreement **0.924**,
**127 of 150** articles labeled identically in all five runs (the "stable subset"
every candidate is scored on).

Production prompt (`summarizer._build_prompt`) and production parser
(`summarizer._parse_summaries`, which enforces index alignment) throughout.

## The ceiling is the whole story

`max_tokens=700` is the incumbent's own ceiling, fitted to Haiku's verbosity.

| @ 700 | gpt-5-nano | deepseek-v4-flash |
|---|---:|---:|
| batches returning empty | 30 / 30 | 26 / 30 |
| alignment failures | 30 / 30 | 27 / 30 |
| articles parsed | **0 / 150** | 15 / 150 |
| output spent reasoning | 100% | 97% |

Zero provider errors in both cases. The failure arrives as an empty string that
fails `index_alignment` — in production that degrades to `_raw_content_fallback`,
writing truncated RSS text as if it were a summary, and the run reports success.

| @ 4000 | gpt-5-nano | deepseek-v4-flash |
|---|---:|---:|
| batches returning empty | 0 | 0 |
| alignment failures | 0 | 0 |
| articles parsed | 150 / 150 | 150 / 150 |
| output spent reasoning | 90% | 83% |

Every `max_tokens` in this repo encodes an assumption that the model does not
think before answering: summarizer 700, linker 500, synthesis `400+120n`,
confirmer `256+60n`. All five were fitted to Haiku.

## Cost — the projection was optimistic by 2.5-7.8x

| model | projected $/1k | measured $/1k | vs projection | vs incumbent | saved/mo |
|---|---:|---:|---:|---:|---:|
| haiku-4-5 (incumbent) | — | 0.491 | — | — | — |
| gpt-5-nano | 0.031 | **0.243** | 7.8x worse | 2.0x cheaper | $14.39 |
| deepseek-v4-flash | 0.044 | **0.111** | 2.5x worse | 4.4x cheaper | $22.05 |

`scripts/project_model_cost.py` re-prices the INCUMBENT's measured token counts,
so it structurally cannot see reasoning overhead. Haiku emits ~330 output tokens
per batch; nano emitted ~2,900 (90% reasoning). The savings are real and roughly
half what was projected.

## Quality — cost-only conclusions do not transfer

Category agreement against the incumbent's stable subset (n=127), where the
incumbent has no run-to-run noise by construction:

| model | agreement | notable |
|---|---:|---|
| deepseek-v4-flash | **0.937** | disagreements scattered |
| gpt-5-nano | 0.898 | **10 of 12 disagreements land on `politics`** |

nano's error is directional, not random — it over-assigns `politics` from
business, health, entertainment and energy. That is a systematic bias and would
reshape the feed's category mix.

**Agreement is not accuracy.** Nothing here reads whether a label is RIGHT. The
incumbent is not ground truth, and a disagreement may be the candidate being
more correct. Deciding that needs gold labels or blind adjudication.

**Summary prose is entirely unmeasured.** Only the category half of this stage
has been evaluated.

## Latency

Per production run (~40 articles = 8 batches), summarizer wall-clock:

| model | p50/batch | p95/batch | run wall-clock |
|---|---:|---:|---:|
| gpt-5-nano | 24.9 s | 31.4 s | ~3.3 min |
| deepseek-v4-flash | 12.5 s | 32.9 s | ~1.7 min |

Against a 30-minute `REFRESH_INTERVAL` for the whole pipeline. Survivable, but a
large increase on the critical path, and it is one stage of eight.

## Where this leaves it

DeepSeek V4 Flash beats gpt-5-nano on **both** axes — cheaper and closer to the
incumbent. It is the candidate worth pursuing.

Nothing here justifies shipping a swap. What it justifies is evaluating summary
QUALITY on DeepSeek, which is the half that decides whether $22/mo is worth
having.
