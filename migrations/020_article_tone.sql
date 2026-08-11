-- Article tone signal (grim | neutral | light), produced by the same Haiku
-- call as why_it_matters + importance_score (services/context_generator.py).
-- NULL = not yet classified; every ranking site treats NULL as neutral
-- (fail-open — a missing signal never penalizes an article).
--
-- Consumed by the feed dampener in sift/lib/db.ts + NewsAggregator.tsx
-- (D48): tone = 'grim' AND importance <= 3 → rank × 0.6. Importance 4-5
-- somber news ranks untouched — the rule de-stacks tabloid crime, it does
-- not hide major news.
--
-- No index: tone appears only inside the computed ORDER BY expression,
-- which was never index-servable.
--
-- Same DDL applied at startup by app/db.py:_apply_migrations (the prod
-- apply path on Railway); this file is documentation + manual ops.

ALTER TABLE articles ADD COLUMN IF NOT EXISTS tone TEXT
    CHECK (tone IN ('grim', 'neutral', 'light'));
