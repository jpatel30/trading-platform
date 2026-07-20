"""
Application configuration loaded from environment variables / .env file.
"""
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

# Absolute path to .env, regardless of the process's CWD. pydantic-settings'
# default env_file=".env" is relative to os.getcwd() at runtime - Claude
# Desktop's MCP subprocess does not reliably chdir to the "cwd" specified
# in claude_desktop_config.json, so a relative path silently finds nothing
# and every field falls back to its class default (empty string for
# mcp_api_key, which has no sensible default - hence "not set" errors).
ENV_FILE = Path(__file__).resolve().parents[2] / ".env"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=ENV_FILE, env_file_encoding="utf-8", extra="ignore")

    # Database
    database_url: str = "postgresql+psycopg2://trading:trading_dev_password@localhost:5432/trading_platform"
    database_url_async: str = "postgresql+asyncpg://trading:trading_dev_password@localhost:5432/trading_platform"

    # Encryption
    encryption_key: str = ""

    # Ollama
    ollama_host: str = "http://localhost:11434"
    ollama_model: str = "qwen2.5:14b"
    ollama_embed_model: str = "nomic-embed-text"

    # ChromaDB
    chromadb_host: str = "http://localhost:8000"

    # Polygon
    polygon_api_key: str = ""

    # Alpha Vantage
    alpha_vantage_api_key: str = ""

    # Unusual Whales API
    unusual_whales_token: str = ""

    # MCP Server
    mcp_server_host: str = "0.0.0.0"
    mcp_server_port: int = 8765
    mcp_api_key: str = ""
    # "stdio" (default): local Claude Desktop subprocess, single user, no
    # bearer auth needed. "http": remote, one shared process serving many
    # customers - each authenticated per-request via ApiKeyTokenVerifier.
    mcp_transport: str = "stdio"


settings = Settings()