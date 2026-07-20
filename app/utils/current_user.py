"""
Resolves "who is the current user" for the MCP server.

Over HTTP transport (remote, one shared server process serving many
customers - see app/mcp_server/auth.py), each request is authenticated
independently against the caller's own bearer token, so identity is read
straight from that per-request context via get_access_token(). Nothing is
cached at module/process level: an earlier version cached the
first-resolved user_id globally, which was safe for a single local stdio
process but would silently leak one customer's identity into every other
customer's calls on a shared remote server.

Over stdio transport (local Claude Desktop subprocess - one user for the
life of the process, no HTTP request to read from), we fall back to
resolving the local MCP_API_KEY (.env) the same way it's always worked.
"""
from fastmcp.server.dependencies import get_access_token

from app.db.queries.user_api_keys import get_user_id_for_api_key
from app.utils.api_keys import hash_api_key
from app.utils.config import settings


def get_current_user_id() -> str:
    """Resolve the current MCP caller's user_id, fresh on every call."""
    access_token = get_access_token()
    if access_token is not None:
        if not access_token.subject:
            raise RuntimeError("Authenticated MCP token has no resolved user_id")
        return access_token.subject

    if not settings.mcp_api_key:
        raise RuntimeError("MCP_API_KEY is not set in .env")

    user_id = get_user_id_for_api_key(hash_api_key(settings.mcp_api_key))
    if not user_id:
        raise RuntimeError("MCP_API_KEY in .env does not match any active user_api_keys record")
    return user_id