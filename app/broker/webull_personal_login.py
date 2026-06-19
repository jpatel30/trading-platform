"""
CLI: Connect Webull personal account (unofficial API) for watchlist access.

The official Webull SDK (App Key/Secret) does NOT expose watchlists.
This uses the unofficial webull library which authenticates with
email/password but we ONLY store the resulting session token — never
the password itself.

Token storage: broker_connections table, broker_name='webull_personal',
               auth_method='token', access_token=encrypted_token_json

Usage (one-time setup):
    python3 -m app.broker.webull_personal_login

After setup, use app.broker.webull_watchlist.get_watchlist_tickers()
to read watchlist tickers without any password.

Token refresh: Webull tokens last ~7 days. Re-run this script to refresh.
"""
import json
import sys
import getpass

from sqlalchemy import text

from app.db.queries.users import get_user_by_email
from app.db.session import get_session
from app.utils.config import settings
from app.utils.crypto import encrypt_token, decrypt_token
from app.utils.current_user import get_current_user_id
from app.db.queries.broker_connections import get_broker_credentials


def save_webull_token(user_id: str, token_data: dict) -> bool:
    """Store encrypted Webull session token in broker_connections."""
    encrypted = encrypt_token(json.dumps(token_data))
    with get_session() as session:
        session.execute(
            text("""
                INSERT INTO broker_connections
                    (user_id, broker_name, auth_method, access_token, is_active, last_synced_at)
                VALUES (:user_id, 'webull_personal', 'token', :token, TRUE, now())
                ON CONFLICT (user_id, broker_name)
                DO UPDATE SET
                    access_token = EXCLUDED.access_token,
                    is_active = TRUE,
                    last_synced_at = now(),
                    updated_at = now()
            """),
            {"user_id": user_id, "token": encrypted}
        )
    return True


def load_webull_token(user_id: str) -> dict | None:
    """Load decrypted Webull session token from DB."""
    creds = get_broker_credentials(user_id, "webull_personal")
    if not creds:
        return None
    try:
        decrypted = decrypt_token(creds[0])
        return json.loads(decrypted)
    except Exception:
        return None


def connect_webull_personal(user_id: str) -> bool:
    """
    Interactive one-time login. Sends MFA code to email first,
    then logs in. Stores ONLY the session token — never the password.
    """
    print("=" * 55)
    print("Webull Personal Account Setup (for watchlist access)")
    print("=" * 55)
    print("Your password is used ONCE to get a session token.")
    print("Only the token is stored (encrypted). Password is never saved.")
    print()

    try:
        import webull as wb
        w = wb.webull()

        email    = input("Webull email: ").strip()
        password = getpass.getpass("Password: ")

        # Step 1: Request MFA code to email (required by Webull)
        print(f"\nSending verification code to {email}...")
        try:
            mfa_result = w.get_mfa(email)
            print(f"Code sent. Check your email inbox/spam.")
        except Exception as e:
            print(f"Warning: Could not send MFA code: {e}")
            print("Will attempt login without MFA...")
            mfa_result = None

        mfa_code = input("Enter the verification code from your email: ").strip()

        # Step 2: Login with password + MFA code
        print("\nLogging in...")
        result = w.login(email, password, mfa=mfa_code)

        # Clear password immediately
        password = None

        # Check login succeeded
        if not result or "accessToken" not in result:
            msg = result.get("msg", result.get("message", str(result))) if result else "Empty response"
            print(f"Login failed: {msg}")
            if "question" in str(result).lower():
                print("\nWebull security question required.")
                print("Log into webull.com manually once to clear the security question, then retry.")
            return False

        print("✅ Login successful!")

        # Extract token data
        token_data = {
            "access_token":  w._access_token,
            "refresh_token": w._refresh_token,
            "token_expire":  w._token_expire,
            "uuid":          getattr(w, "_uuid", ""),
            "did":           getattr(w, "_did", ""),
        }

        # Test watchlist access
        print("\nFetching your watchlists...")
        try:
            watchlists = w.get_watchlists()
            if not watchlists:
                print("⚠️  No watchlists found — create watchlists in the Webull app first.")
            else:
                total = sum(len(wl.get("tickerList", [])) for wl in watchlists)
                print(f"✅ Found {len(watchlists)} watchlist(s), {total} total stocks:")
                for wl in watchlists:
                    symbols = [t.get("symbol") for t in wl.get("tickerList", [])]
                    print(f"   • {wl.get('name', 'Unnamed')}: {', '.join(symbols[:8])}")
                    if len(symbols) > 8:
                        print(f"     ...and {len(symbols)-8} more")
        except Exception as e:
            print(f"⚠️  Could not fetch watchlists: {e}")

        # Save token to DB
        save_webull_token(user_id, token_data)
        print(f"\n✅ Token saved (expires: {token_data.get('token_expire', 'unknown')})")
        print("   Re-run this script in ~7 days to refresh the token.")
        return True

    except ImportError:
        print("webull package not installed. Run: pip install webull")
        return False
    except Exception as e:
        print(f"Login error: {e}")
        return False


def main():
    try:
        user_id = get_current_user_id()
        print(f"Setting up Webull personal account for user: {user_id}")
    except Exception as e:
        print(f"Could not resolve user: {e}")
        sys.exit(1)

    success = connect_webull_personal(user_id)
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()