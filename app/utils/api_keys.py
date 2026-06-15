"""
API key generation and verification.

Keys are generated as random secrets shown ONCE to the user.
Only the SHA-256 hash is stored in the database.
"""
import hashlib
import secrets


def generate_api_key() -> tuple[str, str]:
    """
    Generate a new API key.

    Returns:
        (plaintext_key, hash) — plaintext_key is shown to the user once,
        hash is what gets stored in user_api_keys.api_key_hash
    """
    plaintext = f"tp_{secrets.token_urlsafe(32)}"
    key_hash = hash_api_key(plaintext)
    return plaintext, key_hash


def hash_api_key(plaintext: str) -> str:
    """Hash an API key with SHA-256 for storage/comparison."""
    return hashlib.sha256(plaintext.encode()).hexdigest()


def verify_api_key(plaintext: str, stored_hash: str) -> bool:
    """Check if a plaintext key matches a stored hash."""
    return hash_api_key(plaintext) == stored_hash
