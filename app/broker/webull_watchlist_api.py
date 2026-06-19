"""
Webull Watchlist API Client — Production Ready.

Confirmed working signing formula:
- sign_params: host, x-app-key, x-signature-algorithm, x-signature-nonce,
               x-signature-version, x-timestamp  (+query params if any)
- x-app-secret and x-version: in request headers but NOT signed
- Body: data={} empty for create/refresh; data=json.dumps({...}) for check
- HMAC key: app_secret + "&"
- Nonce: numeric format

Token lifecycle:
    1. create_token()  → PENDING (verify via Webull mobile app SMS)
    2. check_token()   → NORMAL (active, lasts ~15 days)
    3. Auto-refresh 24h before expiry
    4. Stored encrypted in broker_connections (broker_name='webull_market_data')

Watchlist response format:
    GET /watchlist/list         → [{watchlist_id, name, ...}]
    GET /watchlist/instruments  → {instruments: [{symbol, ...}], watchlist_id}
"""
import hashlib
import hmac
import base64
import json
import time
import random
import requests
from datetime import datetime
from urllib.parse import quote
from sqlalchemy import text

from webullsdkcore.utils import common
from app.db.session import get_session
from app.utils.crypto import encrypt_token, decrypt_token
from app.db.queries.broker_connections import get_broker_credentials
from app.utils.current_user import get_current_user_id

BROKER_NAME = "webull_market_data"
PROD_HOST   = "api.webull.com"
UAT_HOST    = "us-openapi-alb.uat.webullbroker.com"


# ─────────────────────────────────────────────────────────────────────────────
# Signing
# ─────────────────────────────────────────────────────────────────────────────

def _sign(
    app_key: str,
    app_secret: str,
    host: str,
    uri: str,
    access_token: str | None = None,
    query_params: dict | None = None,
    body_str: str | None = None,
) -> dict:
    """
    Build signed Webull API request headers.

    sign_params = host + x-app-key + x-signature-algorithm + x-signature-nonce
                + x-signature-version + x-timestamp + query_params (if any)
    x-app-secret and x-version are in headers but NOT signed.
    Body MD5 appended to string-to-sign when body is present.
    """
    nonce = str(int(time.time() * 1000)) + str(random.randint(10**12, 10**13))
    ts    = common.get_iso_8601_date()

    headers = {
        "x-app-key":             app_key,
        "x-app-secret":          app_secret,   # in headers, NOT signed
        "x-timestamp":           ts,
        "x-signature-version":   "1.0",
        "x-signature-algorithm": "HMAC-SHA1",
        "x-signature-nonce":     nonce,
        "x-version":             "v2",          # in headers, NOT signed
        "Accept":                "application/json",
    }
    if access_token:
        headers["x-access-token"] = access_token

    # Sign params: standard set + query params
    sp = {
        "host":                  host,
        "x-app-key":             app_key,
        "x-signature-algorithm": "HMAC-SHA1",
        "x-signature-nonce":     nonce,
        "x-signature-version":   "1.0",
        "x-timestamp":           ts,
    }
    if query_params:
        sp.update({k: str(v) for k, v in query_params.items()})

    parts   = sorted(sp.items())
    sts     = uri + "&" + "&".join(f"{k}={v}" for k, v in parts)

    # Append body MD5 when body is present
    if body_str:
        sts += "&" + hashlib.md5(body_str.encode()).hexdigest().upper()

    encoded = quote(sts, safe="")
    h       = hmac.new((app_secret + "&").encode(), encoded.encode(), hashlib.sha1)
    headers["x-signature"] = base64.b64encode(h.digest()).decode()
    return headers


# ─────────────────────────────────────────────────────────────────────────────
# Token Storage
# ─────────────────────────────────────────────────────────────────────────────

def save_token(user_id: str, token: str, expires_ms: int, host: str) -> None:
    """Save access token + expiry encrypted to DB."""
    payload   = json.dumps({
        "token":      token,
        "expires_ms": expires_ms,
        "host":       host,
        "saved_at":   int(time.time() * 1000),
    })
    encrypted = encrypt_token(payload)
    with get_session() as session:
        session.execute(
            text("""
                INSERT INTO broker_connections
                    (user_id, broker_name, auth_method, access_token, is_active, last_synced_at)
                VALUES (:uid, :broker, 'token', :tok, TRUE, now())
                ON CONFLICT (user_id, broker_name)
                DO UPDATE SET
                    access_token   = EXCLUDED.access_token,
                    is_active      = TRUE,
                    last_synced_at = now(),
                    updated_at     = now()
            """),
            {"uid": user_id, "broker": BROKER_NAME, "tok": encrypted}
        )
    expires_dt = datetime.fromtimestamp(expires_ms / 1000)
    print(f"[Watchlist] Token saved. Expires: {expires_dt.strftime('%Y-%m-%d %H:%M')}")


def load_token(user_id: str) -> dict | None:
    """Load decrypted token data from DB."""
    creds = get_broker_credentials(user_id, BROKER_NAME)
    if not creds:
        return None
    try:
        return json.loads(decrypt_token(creds[0]))
    except Exception:
        return None


def is_expiring_soon(token_data: dict, hours: int = 24) -> bool:
    """Return True if token expires within `hours` hours."""
    return (token_data.get("expires_ms", 0) - int(time.time() * 1000)) < (hours * 3600 * 1000)


# ─────────────────────────────────────────────────────────────────────────────
# Token API
# ─────────────────────────────────────────────────────────────────────────────

def create_token(app_key: str, app_secret: str, host: str) -> dict:
    """Create PENDING token. Verify via Webull mobile app to activate."""
    uri = "/openapi/auth/token/create"
    r   = requests.post(f"https://{host}{uri}",
          headers=_sign(app_key, app_secret, host, uri),
          data={}, timeout=15)
    r.raise_for_status()
    return r.json()


def check_token(app_key: str, app_secret: str, host: str, token: str) -> dict:
    """Check token status (PENDING / NORMAL / EXPIRED). Token goes in JSON body."""
    uri      = "/openapi/auth/token/check"
    body_str = json.dumps({"token": token})
    headers  = _sign(app_key, app_secret, host, uri, body_str=body_str)
    headers["Content-Type"] = "application/json"
    r = requests.post(f"https://{host}{uri}",
        headers=headers, data=body_str, timeout=15)
    r.raise_for_status()
    return r.json()


def refresh_token_api(app_key: str, app_secret: str, host: str, token: str) -> dict:
    """Refresh an expiring token. Token goes in JSON body."""
    uri      = "/openapi/auth/token/refresh"
    body_str = json.dumps({"token": token})
    headers  = _sign(app_key, app_secret, host, uri, body_str=body_str)
    headers["Content-Type"] = "application/json"
    r = requests.post(f"https://{host}{uri}",
        headers=headers, data=body_str, timeout=15)
    r.raise_for_status()
    return r.json()


# ─────────────────────────────────────────────────────────────────────────────
# Smart Token Manager
# ─────────────────────────────────────────────────────────────────────────────

def ensure_valid_token(user_id: str | None = None) -> tuple | None:
    """
    Load token, auto-refresh if expiring within 24h.
    Returns (token, app_key, app_secret, host) or None if setup needed.
    """
    if user_id is None:
        user_id = get_current_user_id()

    token_data = load_token(user_id)
    if not token_data:
        print("[Watchlist] No token found. Run setup_token() first.")
        return None

    creds      = get_broker_credentials(user_id, "webull")
    app_key    = decrypt_token(creds[0])
    app_secret = decrypt_token(creds[1])
    host       = token_data.get("host", PROD_HOST)
    token      = token_data["token"]

    if is_expiring_soon(token_data, hours=24):
        expires_dt = datetime.fromtimestamp(token_data["expires_ms"] / 1000)
        print(f"[Watchlist] Token expiring {expires_dt.strftime('%Y-%m-%d %H:%M')} — refreshing...")
        try:
            result    = refresh_token_api(app_key, app_secret, host, token)
            new_token = result.get("token", token)
            new_exp   = result.get("expires", token_data["expires_ms"])
            save_token(user_id, new_token, new_exp, host)
            token = new_token
            print("[Watchlist] Token refreshed ✅")
        except Exception as e:
            print(f"[Watchlist] Refresh failed: {e}")

    return token, app_key, app_secret, host


def setup_token(host: str = PROD_HOST, user_id: str | None = None) -> bool:
    """One-time setup: create → verify in Webull app → save."""
    if user_id is None:
        user_id = get_current_user_id()

    creds      = get_broker_credentials(user_id, "webull")
    app_key    = decrypt_token(creds[0])
    app_secret = decrypt_token(creds[1])

    print(f"Creating token on {host}...")
    result = create_token(app_key, app_secret, host)
    token  = result.get("token")
    expiry = result.get("expires")
    print(f"Token: {token} | Status: {result.get('status')}")
    print()
    print("Open your Webull mobile app and approve the SMS notification.")
    input("Press Enter once approved...")

    check = check_token(app_key, app_secret, host, token)
    status = check.get("status", check.get("tokenStatus", "UNKNOWN"))
    print(f"Status after verification: {status}")

    if status in ("NORMAL", "ACTIVE", "VERIFIED"):
        save_token(user_id, token, expiry, host)
        print("✅ Token saved and ready.")
        return True

    print(f"❌ Token not active: {check}")
    return False


# ─────────────────────────────────────────────────────────────────────────────
# Watchlist API
# ─────────────────────────────────────────────────────────────────────────────

def _get_watchlists(app_key: str, app_secret: str, host: str, token: str) -> list[dict]:
    uri = "/openapi/market-data/watchlist/list"
    r   = requests.get(f"https://{host}{uri}",
          headers=_sign(app_key, app_secret, host, uri, token), timeout=15)
    r.raise_for_status()
    return r.json()


def _get_instruments(
    app_key: str, app_secret: str, host: str, token: str, watchlist_id: str
) -> list:
    """Returns instruments list from {instruments: [...], watchlist_id: ...}"""
    uri    = "/openapi/market-data/watchlist/instruments/list"
    params = {"watchlist_id": watchlist_id}
    r = requests.get(f"https://{host}{uri}",
        headers=_sign(app_key, app_secret, host, uri, token, query_params=params),
        params=params, timeout=15)
    r.raise_for_status()
    return r.json().get("instruments", [])


def _parse_symbols(instruments: list) -> list[str]:
    """Parse symbol list — handles both dict and string formats."""
    symbols = []
    for i in instruments:
        if isinstance(i, dict):
            sym = i.get("symbol", "")
        else:
            sym = str(i)
        if sym:
            symbols.append(sym)
    return symbols


def get_watchlist_tickers(
    user_id: str | None = None,
    watchlist_name: str | None = None,   # None = use watchlist with most tickers
) -> list[str]:
    """
    Main entry: returns tickers from Webull watchlist.
    Auto-loads stored token, refreshes if expiring.
    Picks the watchlist with most tickers unless watchlist_name is specified.

    Returns [] if no token (run setup_token() first).
    """
    if user_id is None:
        try:
            user_id = get_current_user_id()
        except Exception:
            return []

    result = ensure_valid_token(user_id)
    if not result:
        return []

    token, app_key, app_secret, host = result

    try:
        watchlists = _get_watchlists(app_key, app_secret, host, token)
        best_name, best_symbols = "", []

        for wl in watchlists:
            wl_id   = wl.get("watchlist_id")
            wl_name = wl.get("name", "")
            if not wl_id:
                continue

            # Filter by name if specified
            if watchlist_name and wl_name != watchlist_name:
                continue

            instruments = _get_instruments(app_key, app_secret, host, token, wl_id)
            if not instruments:
                continue

            symbols = _parse_symbols(instruments)
            print(f"[Watchlist] {wl_name}: {len(symbols)} tickers")

            if len(symbols) > len(best_symbols):
                best_symbols = symbols
                best_name    = wl_name

        if best_symbols:
            print(f"[Watchlist] Using '{best_name}' ({len(best_symbols)} tickers)")

        return list(dict.fromkeys(best_symbols))  # deduplicated

    except Exception as e:
        if "401" in str(e):
            print("[Watchlist] Token expired. Run setup_token() to renew.")
        else:
            print(f"[Watchlist] Error: {e}")
        return []