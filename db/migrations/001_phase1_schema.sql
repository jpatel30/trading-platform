-- ============================================================
-- Phase 1 Schema — Week 1
-- Groups 1 & 2: Users & Auth, Broker Connections
-- ============================================================

-- TABLE: users
CREATE TABLE IF NOT EXISTS users (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    email VARCHAR UNIQUE NOT NULL,
    display_name VARCHAR,
    is_active BOOLEAN DEFAULT TRUE,
    invited_by UUID REFERENCES users(id),
    created_at TIMESTAMPTZ DEFAULT now(),
    updated_at TIMESTAMPTZ DEFAULT now()
);

-- TABLE: user_profiles
CREATE TABLE IF NOT EXISTS user_profiles (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID UNIQUE NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    starting_capital DECIMAL,
    max_loss_per_trade DECIMAL,
    max_loss_per_trade_pct DECIMAL,
    max_open_positions INT,
    risk_tolerance VARCHAR CHECK (risk_tolerance IN ('conservative','moderate','aggressive')),
    preferred_strategies JSONB DEFAULT '[]'::jsonb,
    excluded_strategies JSONB DEFAULT '[]'::jsonb,
    preferred_dte_min INT,
    preferred_dte_max INT,
    -- Learned over time (auto-updated)
    avg_hold_days DECIMAL,
    early_exit_rate DECIMAL,
    best_performing_strategy VARCHAR,
    worst_performing_strategy VARCHAR,
    created_at TIMESTAMPTZ DEFAULT now(),
    updated_at TIMESTAMPTZ DEFAULT now()
);

-- TABLE: user_api_keys
CREATE TABLE IF NOT EXISTS user_api_keys (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    api_key_hash VARCHAR(64) NOT NULL,
    label VARCHAR,
    scopes JSONB DEFAULT '["read"]'::jsonb,
    is_active BOOLEAN DEFAULT TRUE,
    last_used_at TIMESTAMPTZ,
    expires_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ DEFAULT now()
);

-- TABLE: invites
CREATE TABLE IF NOT EXISTS invites (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    invited_by UUID REFERENCES users(id),
    email VARCHAR NOT NULL,
    invite_code VARCHAR UNIQUE NOT NULL,
    status VARCHAR DEFAULT 'pending' CHECK (status IN ('pending','accepted','expired')),
    expires_at TIMESTAMPTZ,
    accepted_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ DEFAULT now()
);

-- TABLE: broker_connections
CREATE TABLE IF NOT EXISTS broker_connections (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    broker_name VARCHAR NOT NULL CHECK (broker_name IN ('webull','robinhood')),
    auth_method VARCHAR NOT NULL CHECK (auth_method IN ('oauth2','snaptrade')),
    access_token BYTEA,
    refresh_token BYTEA,
    token_expiry TIMESTAMPTZ,
    snaptrade_user_id VARCHAR,
    snaptrade_user_secret BYTEA,
    is_active BOOLEAN DEFAULT TRUE,
    last_synced_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ DEFAULT now(),
    updated_at TIMESTAMPTZ DEFAULT now(),
    UNIQUE(user_id, broker_name)
);

-- Indexes
CREATE INDEX IF NOT EXISTS idx_user_api_keys_user_id ON user_api_keys(user_id);
CREATE INDEX IF NOT EXISTS idx_broker_connections_user_id ON broker_connections(user_id);
CREATE INDEX IF NOT EXISTS idx_invites_invite_code ON invites(invite_code);
