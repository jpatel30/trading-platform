-- Migration: Add user_watchlist table
-- Run: docker exec -i trading_postgres psql -U trading -d trading_platform < db/migrations/002_watchlist.sql

CREATE TABLE IF NOT EXISTS user_watchlist (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id     UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    ticker      TEXT NOT NULL,
    notes       TEXT DEFAULT '',
    sector      TEXT DEFAULT '',
    added_at    TIMESTAMPTZ DEFAULT now(),
    UNIQUE(user_id, ticker)
);

CREATE INDEX IF NOT EXISTS idx_watchlist_user ON user_watchlist(user_id);