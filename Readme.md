# Trading Intelligence Platform

AI-powered options and stock recommendation system. Analyzes your own
watchlist using real-time market data (flow, dark pool, institutional
options positioning, VIX term structure) and generates trade
recommendations with entry/exit levels via Claude Desktop (MCP) and a
web dashboard.

Stack: Python 3.11, FastMCP, PostgreSQL, Ollama (Qwen 14B),
Unusual Whales API, Polygon/Massive API, Next.js dashboard (StockBros)

---

## What's Built

| Feature | Status |
|---|---|
| Watchlist-driven scanner, no broker connection required | Done |
| Admin-curated default watchlist + per-user personal additions | Done |
| Smart multi-horizon recommendations (options + stock, 1 LLM call) | Done |
| Options flow / dark pool scoring (real signal, not zeroed) | Done |
| OI buildup signal - multi-day institutional accumulation, leading not lagging | Done |
| Market regime (VIX term structure + put/call ratio) | Done |
| Real risk/reward gate + probability-adjusted EV gate on trade math | Done |
| Conviction scoring (flow + TA + IV + institutional) | Done |
| Position monitor + Discord alerts (stop-loss AND take-profit) | Done |
| Real target/stop tracked per fill (not a generic default) | Done |
| Mark-to-market P&L, wired into the nightly learning loop | Done |
| Sell signal detection (rule-based exit triggers) | Done |
| End-of-day learning loop (auto-resumes on restart) | Done |
| 59 MCP tools for Claude Desktop | Done |
| StockBros web dashboard (Next.js, mobile-responsive) | Done |
| Frontend watchlist-mode toggle (Default / Default + Mine) | Pending |
| Predictive IV-expansion signal (see below) | Pending |

---

## Broker Connection Is Optional

Webull is used for the admin's own live portfolio, positions, and
order history - it is NOT required to generate recommendations.
The scanner and recommendation engine read entirely from your own
watchlist in the database; anyone can use the platform with zero
broker connection. This changed in July 2026 - earlier versions
required a live Webull connection to build a scan universe at all.

---

## Quick Start

### Prerequisites
- macOS (Apple Silicon recommended) or Ubuntu 22+
- Python 3.11+, Docker Desktop, Ollama, Claude Desktop
- Unusual Whales API token (paid plan, 120 req/min)
- Polygon/Massive API key (free tier sufficient)
- Webull Developer API access - optional, only needed for live
  portfolio/trading features, not for recommendations

### Setup

```
git clone https://github.com/jpatel30/trading-platform
cd trading-platform
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt

cp .env.example .env

docker compose up -d
ollama pull qwen2.5:14b

python3 -m app.db.migrations
```

Note: several tables/columns added since these migration files were
last updated (daily_recommendations fill-tracking columns,
excluded_from_stats, users.is_admin, etc.) exist only as ad-hoc SQL
run directly against the live DB this session - not yet captured as
versioned migrations. See REMAINING_ITEMS.md.

Seed your watchlist (add tickers via the dashboard's watchlist page,
or directly into user_watchlist), then mark yourself admin:

```
docker exec trading_postgres psql -U trading -d trading_platform -c "UPDATE users SET is_admin = TRUE WHERE id = (SELECT id FROM users LIMIT 1);"
```

Start the servers:

```
python3 -m app.mcp_server.server
uvicorn app.api.main:app --port 8001 --reload
```

### Claude Desktop Config

Add to claude_desktop_config.json:

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

Key .env variables:

```
DATABASE_URL=postgresql+psycopg2://trading:password@localhost:5432/trading_platform
ENCRYPTION_KEY=...              Fernet key for broker token encryption
OLLAMA_HOST=http://localhost:11434
OLLAMA_MODEL=qwen2.5:14b
WEBULL_CLIENT_ID=...             Optional - only for live portfolio/trading
WEBULL_CLIENT_SECRET=...         Optional - only for live portfolio/trading
POLYGON_API_KEY=...              Free tier sufficient (grouped_daily only)
UNUSUAL_WHALES_TOKEN=...         Paid plan required (120 req/min)
MCP_API_KEY=...                  Auth for MCP server (single-user today)
```

---

## Data Sources

| Source | Plan | Usage | Speed |
|---|---|---|---|
| Unusual Whales | Paid ($120/mo) | OHLC, live price, IV rank, flow, dark pool, earnings, news, GEX, OI change | 0.12-0.15s/call |
| Polygon/Massive | Free | All-ticker daily price (1 batch call) | 2s/call |
| yfinance | Free | VIX, analyst targets, fundamentals, momentum | 0.3-1s |
| Ollama Qwen 14B | Local | Strategy decisions, thesis generation | 16-18s/call |

Unusual Whales' economic calendar and net-flow-by-expiry endpoints are
not yet used - see REMAINING_ITEMS.md.

---

## Architecture

See ARCHITECTURE.md for full system design, data flow, and component details.

See MCP_TOOLS.md for all 59 MCP tools with parameters and examples.

See REMAINING_ITEMS.md for open work.

---

## Performance

- Scanner (watchlist-sized, ~130 tickers): 2-4s
- Full options rescan (enrichment + LLM + trade math): 60-80s. This
  grew from an earlier ~26s baseline as OI buildup, market regime, and
  richer per-ticker enrichment were added; the LLM call itself is the
  majority of this time.
- Stock scan: 20-35s
- Portfolio load (cached): under 1s
- Position monitor: 15-min cycle, auto-resumes on server restart

---

## Recent Major Fixes (July 2026)

- Options flow and dark pool scoring were silently zeroed since day
  one (wrong field name checked) - found independently in 5 separate
  files, now centralized in app/signals/flow_scoring.py.
- The risk/reward gate on trade math checked a constant ratio, not
  real strike economics - a credit trade could pass with actual R/R
  far worse than displayed. Fixed, plus a new probability-adjusted EV
  gate for debit strategies.
- Mark-to-market P&L% for credit strategies (iron condors) divided by
  the wrong denominator (credit received instead of capital at risk),
  inflating returns by roughly the margin/credit ratio.
- The scanner used to require a live Webull connection to build a
  watchlist; it's now entirely database-driven with no hardcoded
  fallback lists anywhere in the pipeline.
- strategy_recommendations, an older parallel table for fill/outcome
  tracking, has been fully retired in favor of daily_recommendations -
  see ARCHITECTURE.md.

---

## License

Personal use only. Unusual Whales API terms restrict commercial redistribution of their data.