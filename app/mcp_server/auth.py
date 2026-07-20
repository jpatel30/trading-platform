"""
Per-request bearer-token verification for remote MCP access.

Under HTTP transport, FastMCP calls verify_token() independently for each
incoming request and attaches the result to that request's scope - nothing
is shared or cached across requests, so one server process can safely serve
many simultaneous customers, each authenticated by their own token, without
identity leaking between them.

Reuses the exact hash-and-lookup mechanism the local MCP_API_KEY has always
used: app.utils.api_keys.hash_api_key() + the user_api_keys table via
app.db.queries.user_api_keys.get_user_id_for_api_key(). A customer's key is
minted the same way the admin's original key was - generate_api_key() +
create_api_key() - just per-customer instead of once at setup time.
"""
from fastmcp.server.auth import TokenVerifier
from fastmcp.server.auth.auth import AccessToken

from app.db.queries.user_api_keys import get_user_id_for_api_key
from app.utils.api_keys import hash_api_key


class ApiKeyTokenVerifier(TokenVerifier):
    """Verifies an MCP bearer token against user_api_keys, per request."""

    async def verify_token(self, token: str) -> AccessToken | None:
        user_id = get_user_id_for_api_key(hash_api_key(token))
        if not user_id:
            return None
        return AccessToken(token=token, client_id=user_id, scopes=[], subject=user_id)
