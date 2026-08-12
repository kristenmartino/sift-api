-- Funding edges between organizations, from IRS 990 filings.
--
-- Sift's dossiers describe organizations one at a time. This table holds the
-- relationships *between* them: grants paid (Schedule I Part II) and declared
-- related tax-exempt organizations (Schedule R Part II). Both are self-reported
-- by the payer/declarer and open to public inspection, so every row is a filed
-- fact rather than an inference — `reported_by` is always 'source' for that
-- reason, and there is deliberately no column for a computed or assumed edge.
--
-- ein_name_agrees is the load-bearing column. The counterparty EIN is a join
-- key typed by a human at the filing organization, and humans mistype: in the
-- first ten-org pull, Brookings listed "Urban League of Louisiana" under The
-- Urban Institute's EIN. Joining on the EIN alone would produce a confidently
-- wrong, fully-cited edge. So at ingest each edge's filed name is compared
-- against the IRS's own name for that EIN (from the annual index CSV) and the
-- verdict is stored:
--
--   agrees      the filing and the IRS agree on who this EIN is  -> publishable
--   review      they disagree; a human has not adjudicated yet   -> withheld
--   ein_absent  EIN not in the index (LLC, government, non-filer) -> withheld
--
-- 'review' does not mean 'wrong'. Harvard Law School filed under President and
-- Fellows of Harvard College is legitimate; Urban League under Urban Institute
-- is an error; no string comparison distinguishes them, so both are held.
--
-- Applied in prod by app/db.py:_apply_migrations at startup; this file is
-- documentation + manual ops.

CREATE TABLE IF NOT EXISTS funding_edges (
    id                   BIGSERIAL PRIMARY KEY,
    source_ein           TEXT NOT NULL,
    source_name          TEXT NOT NULL,
    target_ein           TEXT,
    -- Verbatim as filed. Never overwritten with the IRS spelling: the filed
    -- string is the evidence, and a reviewer needs to see what was actually
    -- written next to what the IRS says it should be.
    target_name_as_filed TEXT,
    target_name_irs      TEXT,
    edge_kind            TEXT NOT NULL CHECK (edge_kind IN ('grant', 'related_org')),
    amount_usd           BIGINT,
    purpose              TEXT,
    exempt_code          TEXT,
    fiscal_period        TEXT NOT NULL,          -- YYYYMM of the filing's tax period
    form                 TEXT NOT NULL,          -- '990 Sch I Part II' | '990 Sch R Part II'
    reported_by          TEXT NOT NULL DEFAULT 'source',
    match_method         TEXT NOT NULL DEFAULT 'ein',
    ein_name_agrees      TEXT NOT NULL
                         CHECK (ein_name_agrees IN ('agrees', 'review', 'ein_absent')),
    object_id            TEXT NOT NULL,          -- IRS e-file object id
    filing_url           TEXT NOT NULL,
    retrieved_at         TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- One edge per (filer, counterparty, period, form, amount). Amount is in the
-- key because a filer may report several grants to the same recipient in one
-- year for different purposes; COALESCE keeps related-org rows (NULL amount)
-- from colliding with each other.
CREATE UNIQUE INDEX IF NOT EXISTS idx_funding_edges_identity
    ON funding_edges (source_ein, target_ein, fiscal_period, form, COALESCE(amount_usd, -1));

-- The read path filters on the verdict, so it leads the index.
CREATE INDEX IF NOT EXISTS idx_funding_edges_publishable
    ON funding_edges (ein_name_agrees, target_ein);

CREATE INDEX IF NOT EXISTS idx_funding_edges_source
    ON funding_edges (source_ein, fiscal_period DESC);
