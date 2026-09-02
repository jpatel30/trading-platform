# Trading Intelligence Platform

AI-powered options recommendation system (options prediction is the focus;
stock picking is a secondary, legacy-style feature). Scans your own
watchlist against real-time market data — options flow, dark pool,
institutional positioning, VIX term structure, SEC filings — and produces
specific trade recommendations (strategy, strikes, expiry, entry/target/stop)
via a 6-agent pipeline, then tracks every recommendation through paper-trade
open/close and feeds outcomes back into a learning loop. Accessible via
Claude Desktop (MCP) and a web dashboard.

Stack: Python 3.11, FastAPI, FastMCP, PostgreSQL, ChromaDB, Ollama (local
LLM, qwen2.5:14b), Unusual Whales API, Polygon API, yfinance, Next.js
dashboard (StockBros, separate repo).

No broker account linking exists anywhere in the system — removed
entirely 2026-08-10, for every user including the former admin. The
scanner, recommendations, and fill-tracking are 100% database-driven.

See **ARCHITECTURE.md** for full system design, the agent pipeline, data
flow, and the current remaining-work list — this file stays a quick
overview + setup guide, not a design doc.

---

## What's Built

| Feature | Status |
|---|---|
| Watchlist-driven scanner, no broker connection required or possible | Done |
| Admin-curated default watchlist + per-user personal additions | Done |
| Multi-agent options pipeline (Search -> News -> Prediction routing -> Bull/Bear debate -> Finalize) | Done |
| SEC filing RAG (ChromaDB) feeding the Bull/Bear debate | Done |
| Smart multi-horizon stock recommendations (single LLM call, separate legacy-style pipeline) | Done |
| Options flow / dark pool scoring (real signal, not zeroed) | Done |
| OI buildup signal - multi-day institutional accumulation, leading not lagging | Done |
| Market regime (VIX term structure + put/call ratio) | Done |
| Predictive IV-expansion signal (anti-chasing filter, Signal 6 in quick_scan) | Done |
| Real risk/reward gate + probability-adjusted EV gate on trade math | Done |
| Conviction scoring (flow + TA + IV + institutional) | Done |
| Position monitor + Discord alerts (stop-loss AND take-profit) | Done |
| Real target/stop tracked per fill (not a generic default) | Done |
| Mark-to-market P&L, wired into the nightly learning loop | Done |
| Sell signal detection (rule-based exit triggers) | Done |
| Nightly + rolling-7-day weekly learning loop (auto-resumes on restart) | Done |
| 59 MCP tools for Claude Desktop | Done |
| StockBros web dashboard (Next.js, mobile-responsive) | Done |
| Learning Agent split into its own standalone service (still inline in main.py) | Pending |
| Frontend watchlist-mode toggle (Default / Default + Mine) | Pending |

---

## Quick Start

### Prerequisites
- macOS (Apple Silicon recommended) or Ubuntu 22+
- Python 3.11+, Docker Desktop, Ollama, Claude Desktop
- Unusual Whales API token (paid plan, 120 req/min)
- Polygon API key (free tier sufficient)

No broker credentials of any kind are needed — there's no broker
integration left in the system.

### Setup

```
git clone https://github.com/jpatel30/trading-platform
cd trading-platform
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt

# no .env.example exists yet (open item) - create .env yourself using
# the variables listed under Configuration below

docker compose up -d          # postgres + chromadb
ollama pull qwen2.5:14b

python3 -m app.db.migrations
```

Note: several tables/columns added since these migration files were last
updated (fill-tracking columns, the multi-agent pipeline's tables, etc.)
exist only as ad-hoc SQL run directly against the live DB — not yet
captured as versioned migrations. See ARCHITECTURE.md.

Seed your watchlist (add tickers via the dashboard's watchlist page, or
directly into `user_watchlist`), then mark yourself admin:

```
docker exec trading_postgres psql -U trading -d trading_platform -c "UPDATE users SET is_admin = TRUE WHERE id = (SELECT id FROM users LIMIT 1);"
```

Start everything (FastAPI + the 5 agent services it spawns/supervises):

```
bash runbook.sh start
```

Or start the MCP server separately for Claude Desktop:

```
python3 -m app.mcp_server.server
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
ENCRYPTION_KEY=...              Fernet key for any encrypted-at-rest fields
OLLAMA_HOST=http://localhost:11434
OLLAMA_MODEL=qwen2.5:14b
OLLAMA_EMBED_MODEL=nomic-embed-text
POLYGON_API_KEY=...              Free tier sufficient (grouped_daily only)
UNUSUAL_WHALES_TOKEN=...         Paid plan required (120 req/min)
MCP_API_KEY=...                  Auth for MCP server (single-user today)
```

---

## Data Sources

| Source | Plan | Usage | Speed |
|---|---|---|---|
| Unusual Whales | Paid ($120/mo) | OHLC, live price, IV rank, flow, dark pool, earnings, news, GEX, OI change, economic calendar, net-flow-by-expiry | 0.12-0.15s/call |
| Polygon | Free | All-ticker daily price (1 batch call) | 2s/call |
| yfinance | Free | VIX, analyst targets, fundamentals, momentum | 0.3-1s |
| SEC EDGAR | Free | 8-K filings, chunked + embedded into ChromaDB | — |
| Ollama (local, qwen2.5:14b) | Free | Bull/Bear debate, Prediction routing narrative, Learning weekly review, strategy decisions | 16-18s/call |

---

## Performance

- Scanner (watchlist-sized, ~130 tickers): 2-4s
- On-demand/manual options rescan (enrichment + LLM + trade math,
  `rescan_engine.py` - still used by the web UI's manual rescan button and
  the MCP tool; separate from the automated multi-agent pipeline below):
  60-80s. The LLM call itself is the majority of this time.
- The automated multi-agent options pipeline (Search/News/Prediction/
  Bull/Bear) runs on its own cron schedule as background services, not
  synchronously in response to a user action - see ARCHITECTURE.md for
  the exact trigger times.
- Stock scan: 20-35s
- Position monitor: 15-min cycle, auto-resumes on server restart

---

## License

Personal use only. Unusual Whales API terms restrict commercial redistribution of their data.
