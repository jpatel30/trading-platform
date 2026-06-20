CREATE TABLE IF NOT EXISTS sell_recommendations (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id         UUID REFERENCES users(id) ON DELETE CASCADE,
    symbol          TEXT NOT NULL,
    instrument_type TEXT DEFAULT 'STOCK',
    recommended_at  TIMESTAMPTZ DEFAULT now(),
    cost_basis      NUMERIC,
    market_value    NUMERIC,
    pnl_pct         NUMERIC,
    rule_signals    JSONB,
    llm_action      TEXT,
    llm_exit_pct    INTEGER,
    llm_summary     TEXT,
    llm_confidence  TEXT,
    user_acted      BOOLEAN,
    actual_exit_pct INTEGER,
    exit_price      NUMERIC,
    outcome_pnl     NUMERIC,
    outcome_at      TIMESTAMPTZ,
    was_correct     BOOLEAN
);
CREATE INDEX IF NOT EXISTS idx_sell_rec_user ON sell_recommendations(user_id, recommended_at DESC);
