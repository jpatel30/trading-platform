"""
Resolves "who is the current user" for the MCP server (Phase 1-3).

Claude Desktop launches the MCP server as a local subprocess - there's no
HTTP request/session/JWT to extract identity from. This MCP server
instance is bound to ONE user via MCP_API_KEY (.env), which was generated
and stored (hashed, SHA-256) in user_api_keys for that user at setup time.

This is the SAME api-key-auth mechanism described in the security
architecture (Section 7) - just used locally for now. If the MCP server
is later exposed remotely, the same MCP_API_KEY becomes a real bearer
credential rather than a local config value - no change needed here.

Phase 4+ Web UI: user_id comes from session/JWT after login instead -
this resolver is NOT used by the API layer.
"""
from app.db.queries.user_api_keys import get_user_id_for_api_key
from app.utils.api_keys import hash_api_key
from app.utils.config import settings

_cached_user_id: str | None = None


def get_current_user_id() -> str:
    """Resolve the local MCP user's id from MCP_API_KEY (.env)."""
    global _cached_user_id
    if _cached_user_id:
        return _cached_user_id

    if not settings.mcp_api_key:
        raise RuntimeError("MCP_API_KEY is not set in .env")

    user_id = get_user_id_for_api_key(hash_api_key(settings.mcp_api_key))
    if not user_id:
        raise RuntimeError("MCP_API_KEY in .env does not match any active user_api_keys record")

    _cached_user_id = user_id
    return _cached_user_id