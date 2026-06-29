# Trading Intelligence Platform

AI-powered options and stock recommendation system. Connects to Webull via API, analyzes 127+ tickers using real-time market data, and generates trade recommendations with entry/exit levels via Claude Desktop (MCP) and a web dashboard.

**Stack:** Python 3.11 · FastMCP · PostgreSQL · Ollama (Qwen 14B) · Unusual Whales API · Polygon/Massive API · Next.js dashboard (StockBros)

---

## What's Built

| Feature | Status |
|---|---|
| Live Webull portfolio (positions, balances, orders) | ✅ |
| 127-ticker watchlist scanner (1.2s via batch UW calls) | ✅ |
| Smart multi-horizon recommendations (1 LLM call ~26s) | ✅ |
| Options: all expiries — LLM picks best one | ✅ |
| Stocks: 3m / 6m / 1yr with analyst targets | ✅ |
| Conviction scoring (flow + TA + IV + institutional) | ✅ |
| Position monitor + Discord alerts | ✅ |
| Sell signal detection (-40%/-50%/-100% triggers) | ✅ |
| End-of-day learning (tracks recommendation accuracy) | ✅ |
| 48+ MCP tools for Claude Desktop | ✅ |
| StockBros web dashboard (Next.js, mobile-responsive) | ✅ |

---

## Quick Start

### Prerequisites
- macOS (Apple Silicon recommended) or Ubuntu 22+
- Python 3.11+ · Docker Desktop · Ollama · Claude Desktop
- Webull account with Developer API access
- Unusual Whales API token (paid plan, 120 req/min)
- Polygon/Massive API key (free tier sufficient)

### Setup

```bash
# 1. Clone and install
git clone https://github.com/jpatel30/trading-platform
cd trading-platform
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt

# 2. Copy and fill environment
cp .env.example .env

# 3. Start infrastructure
docker compose up -d          # Postgres + ChromaDB
ollama pull qwen2.5:14b       # Local LLM (~9GB)

# 4. Run migrations
python3 -m app.db.migrations

# 5. Health check
bash health_check.sh

# 6. Start MCP server
python3 -m app.mcp_server.server

# 7. Start FastAPI (for dashboard)
uvicorn app.api.main:app --port 8001 --reload
```

### Claude Desktop Config

Add to `~/Library/Application\ Support/Claude/claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "trading": {
      "command": "python3",
      "args": ["-m", "app.mcp_server.server"],
      "cwd": "/path/to/trading-platform",
      "env": { "PYTHONPATH": "/path/to/trading-platform" }
    }
  }
}
```

---

## Configuration

Key `.env` variables:

```env
DATABASE_URL=postgresql+psycopg2://trading:password@localhost:5432/trading_platform
ENCRYPTION_KEY=...              # Fernet key for broker token encryption
OLLAMA_HOST=http://localhost:11434
OLLAMA_MODEL=qwen2.5:14b
WEBULL_CLIENT_ID=...
WEBULL_CLIENT_SECRET=...
POLYGON_API_KEY=...             # Free tier sufficient (grouped_daily only)
UNUSUAL_WHALES_TOKEN=...        # Paid plan required (120 req/min)
MCP_API_KEY=...                 # Optional auth for MCP server
```

---

## Data Sources

| Source | Plan | Usage | Speed |
|---|---|---|---|
| Unusual Whales | Paid ($120/mo) | OHLC bars, live price, IV rank, flow, dark pool, earnings, news, GEX | 0.12-0.15s/call |
| Polygon/Massive | Free | All-ticker daily price (1 batch call for 127 tickers) | 2s/call |
| yfinance | Free | VIX, analyst targets, fundamentals | 0.3-1s |
| Ollama Qwen 14B | Local | Strategy decisions, thesis generation | 16-18s/call |

---

## Architecture

See [ARCHITECTURE.md](ARCHITECTURE.md) for full system design, data flow, and component details.

See [MCP_TOOLS.md](MCP_TOOLS.md) for all 48 MCP tools with parameters and examples.

---

## Performance

- **Scanner:** 127 tickers in 1.2s (2 batch UW calls instead of 254 individual)
- **Smart recommendations:** 26s total (1 LLM call for all horizons)
- **Portfolio load:** <1s (cache-first)
- **On weekday market hours:** ~20-22s (fewer 429s, richer flow data)

---

## License

Personal use only. Unusual Whales API terms restrict commercial redistribution of their data.
