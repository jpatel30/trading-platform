# Personal Trading Intelligence Platform

A self-improving, multi-user options trading intelligence system that evolves from a portfolio tracker into an autonomous trading algorithm — personalized to each user's risk tolerance, budget, and execution behavior.

**Architecture:** MCP-first (Claude Desktop as client for Phases 1-3) → Custom Dashboard UI (Phase 4+)  
**Status:** Phase 1 complete — live Webull portfolio tracking via Claude Desktop

---

## What's Built (Phase 1)

- **Live portfolio tracking** — positions (stocks + options), balances, today's orders via Webull API
- **MCP server** — 4 tools: `ping`, `get_positions`, `get_balances`, `get_orders`
- **Multi-user DB schema** — users, profiles, API keys, broker connections (all encrypted)
- **Claude Desktop integration** — ask "What are my positions?" and get live Webull data

---

## Prerequisites

| Requirement | Version | Notes |
|---|---|---|
| macOS | 12+ | Apple Silicon (M1/M2/M3/M4/M5) recommended |
| Python | 3.11+ | Install via pyenv (see below) |
| Docker Desktop | Latest | For Postgres, ChromaDB, Ollama |
| Claude Desktop | Latest | For MCP integration |
| Webull account | — | With Developer API access |

---

## Local Setup

### 1. Clone the repo

```bash
git clone <repo-url>
cd trading-platform
```

### 2. Install Python 3.11 via pyenv

```bash
brew install pyenv
echo 'export PYENV_ROOT="$HOME/.pyenv"' >> ~/.zshrc
echo '[[ -d $PYENV_ROOT/bin ]] && export PATH="$PYENV_ROOT/bin:$PATH"' >> ~/.zshrc
echo 'eval "$(pyenv init -)"' >> ~/.zshrc
source ~/.zshrc
brew install xz
pyenv install 3.11.9
```

### 3. Create virtual environment

```bash
python3 -m venv venv
source venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
```

### 4. Start the Docker stack

```bash
docker compose up -d
docker compose ps  # confirm all 3 services are healthy
```

Services started:
- **Postgres 16** on `localhost:5432`
- **ChromaDB** on `localhost:8000`
- **Ollama** on `localhost:11434` (Qwen2.5:14b + nomic-embed-text for Phase 2+)

### 5. Pull Ollama models

```bash
docker exec trading_ollama ollama pull qwen2.5:14b      # ~9GB, reasoning model
docker exec trading_ollama ollama pull nomic-embed-text  # ~270MB, embeddings
```

### 6. Create the database schema

```bash
docker exec -i trading_postgres psql -U trading -d trading_platform \
  < db/migrations/001_phase1_schema.sql
```

### 7. Set up environment variables

```bash
cp .env.example .env
```

Edit `.env` and fill in:

```env
# Required for Phase 1
ENCRYPTION_KEY=        # generate: python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
MCP_API_KEY=           # generated in step 9 below

# Required for Phase 2 (market data)
POLYGON_API_KEY=       # from polygon.io
```

> ⚠️ **Never commit `.env`** — it contains your encryption key. `.gitignore` excludes it.

### 8. Create your user + store Webull credentials

**Create user:**
```bash
docker exec trading_postgres psql -U trading -d trading_platform -c \
  "INSERT INTO users (email, display_name) VALUES ('you@example.com', 'Your Name') RETURNING id;"
```
Note the returned UUID — you'll need it below.

**Create a profile:**
```bash
docker exec trading_postgres psql -U trading -d trading_platform -c \
  "INSERT INTO user_profiles (user_id, starting_capital, risk_tolerance, max_open_positions)
   VALUES ('<user_id>', 50000, 'moderate', 8);"
```

**Register your Webull App Key + Secret:**

First, generate your Webull API credentials:
1. Go to [developer.webull.com](https://developer.webull.com)
2. Create an app with **Market Data** + **Trading** permissions + **2FA enabled**
3. Note your App Key and App Secret

Then register them (encrypted) in the DB:
```bash
python3 -m app.broker.connect_broker you@example.com webull <APP_KEY> <APP_SECRET>
```

### 9. Generate your MCP API key

```bash
python3 -c "
from app.utils.api_keys import generate_api_key
from app.db.queries.user_api_keys import create_api_key
plain, hashed = generate_api_key()
key_id = create_api_key('<user_id>', hashed, label='mcp-primary')
print('MCP_API_KEY:', plain)
"
```

Copy the printed key into `.env` as `MCP_API_KEY=<value>`.

### 10. Verify everything works

```bash
python3 -c "
from app.utils.current_user import get_current_user_id
from app.broker.webull_connector import WebullConnector
user_id = get_current_user_id()
print('User ID:', user_id)
wb = WebullConnector(user_id)
print('Positions:', len(wb.get_positions()))
print('Balance keys:', list(wb.get_balance().keys()))
"
```

---

## Connect to Claude Desktop

### 1. Find Claude Desktop config

```
~/Library/Application Support/Claude/claude_desktop_config.json
```

### 2. Add the MCP server entry

```json
{
  "mcpServers": {
    "trading-platform": {
      "command": "/absolute/path/to/trading-platform/venv/bin/python3",
      "args": ["/absolute/path/to/trading-platform/app/mcp_server/server.py"],
      "cwd": "/absolute/path/to/trading-platform"
    }
  }
}
```

Replace paths with your actual absolute paths (run `pwd` in the project folder to get them).

### 3. Restart Claude Desktop

Fully quit (Cmd+Q) and reopen. In a new chat, try:
- *"Use the ping tool to check the trading platform connection"*
- *"What are my current positions?"*
- *"What's my account balance?"*
- *"What orders did I place today?"*

---

## Webull US API Notes

The Webull Python SDK ships v1 and v2 endpoints. **All v2 endpoints are unsupported for US accounts** as of June 2026 (HK/JP only, per SDK docstrings). Use v1 exclusively:

| Action | Endpoint |
|---|---|
| Discover account ID | `api.account.get_app_subscriptions()` |
| Get positions | `api.account.get_account_position(account_id)` |
| Get balance | `api.account.get_account_balance(account_id, 'USD')` |
| Get today's orders | `api.order.list_today_orders(account_id)` |

Also note: option position `market_value` / `last_price` from the API are **per-share** (not per-contract). The connector applies the `×100` multiplier automatically for `instrument_type == 'OPTION'`.

---

## Project Structure

```
trading-platform/
├── app/
│   ├── broker/
│   │   ├── base.py              # BrokerConnector interface + BrokerNotConnectedError
│   │   ├── webull_connector.py  # Webull implementation (WebullConnector)
│   │   └── connect_broker.py    # CLI: register broker credentials for a user
│   ├── db/
│   │   ├── session.py           # SQLAlchemy engine + get_session() context manager
│   │   └── queries/
│   │       ├── users.py         # get_user_by_email, get_user_by_id
│   │       ├── broker_connections.py  # get/upsert/deactivate broker credentials
│   │       └── user_api_keys.py # get_user_id_for_api_key, create_api_key
│   ├── mcp_server/
│   │   └── server.py            # FastMCP server: ping, get_positions, get_balances, get_orders
│   └── utils/
│       ├── config.py            # pydantic-settings (absolute .env path)
│       ├── crypto.py            # Fernet encrypt/decrypt for broker tokens
│       ├── api_keys.py          # SHA-256 API key generation + verification
│       └── current_user.py      # Resolves MCP_API_KEY -> user_id
├── db/
│   └── migrations/
│       └── 001_phase1_schema.sql  # All 5 Phase 1 tables
├── docker-compose.yml             # Postgres + ChromaDB + Ollama
├── requirements.txt
└── .env.example
```

---

## Roadmap

| Phase | Name | Status | Description |
|---|---|---|---|
| 1 | SEE | ✅ Complete | Portfolio tracking, live P&L, Webull integration, MCP server |
| 2 | THINK | 🔄 In progress | Options scanner (3000+ tickers), strategy engine, Polygon.io, RAG |
| 3 | LEARN | ⏳ Planned | Prediction tracking, win/loss analysis, self-improving weights |
| 4 | ACT | ⏳ Planned | Trade execution, Next.js dashboard, multi-user UI |

---

## Adding a New User (Multi-User)

```bash
# 1. Create user record
docker exec trading_postgres psql -U trading -d trading_platform -c \
  "INSERT INTO users (email, display_name) VALUES ('newuser@example.com', 'New User') RETURNING id;"

# 2. Register their Webull credentials (they generate their own App Key/Secret)
python3 -m app.broker.connect_broker newuser@example.com webull <THEIR_APP_KEY> <THEIR_APP_SECRET>

# 3. Generate their MCP API key
python3 -c "
from app.utils.api_keys import generate_api_key
from app.db.queries.user_api_keys import create_api_key
plain, hashed = generate_api_key()
create_api_key('<new_user_id>', hashed, label='mcp-primary')
print('Give this key to the user for their .env MCP_API_KEY:', plain)
"
```

Each user runs their own MCP server instance locally, bound to their own `MCP_API_KEY` in `.env`.

---

## Security Notes

- Broker App Key/Secret are encrypted with Fernet before DB storage (`ENCRYPTION_KEY` env var only, never in DB)
- API keys stored as SHA-256 hashes only (plaintext shown once at generation, never persisted)
- All DB queries filter by `user_id` — no cross-user data access
- Read-only Webull access by default (trade execution added in Phase 4 with explicit user re-authorization)
- See `trading_platform_complete_blueprint.pdf` for full security architecture (Section 7)