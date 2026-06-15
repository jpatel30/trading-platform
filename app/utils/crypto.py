"""
Encryption helpers for broker tokens at rest (pgcrypto-equivalent at app layer).

Uses Fernet symmetric encryption. The key comes from ENCRYPTION_KEY env var
and must NEVER be stored in the database or committed to source control.
"""
from cryptography.fernet import Fernet

from app.utils.config import settings


def _get_fernet() -> Fernet:
    if not settings.encryption_key:
        raise RuntimeError(
            "ENCRYPTION_KEY is not set in .env. "
            "Generate one with: python3 -c \"from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())\""
        )
    return Fernet(settings.encryption_key.encode())


def encrypt_token(plaintext: str) -> bytes:
    """Encrypt a token (e.g. OAuth access/refresh token) for DB storage."""
    return _get_fernet().encrypt(plaintext.encode())


def decrypt_token(ciphertext: bytes | memoryview) -> str:
    """
    Decrypt a token retrieved from the DB.

    psycopg2 returns BYTEA columns as `memoryview`, not `bytes` - Fernet
    only accepts bytes/str, so coerce here. This protects ALL callers
    (broker credentials now; any future encrypted fields later).
    """
    if isinstance(ciphertext, memoryview):
        ciphertext = ciphertext.tobytes()
    return _get_fernet().decrypt(ciphertext).decode()