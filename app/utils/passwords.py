"""
User password hashing — bcrypt, not the plain SHA-256 api_keys.py uses
for API keys. API keys are random, high-entropy strings (brute-forcing
one is infeasible regardless of hash speed); passwords are human-chosen
and low-entropy, so they need a deliberately SLOW, salted hash to
resist offline brute-force/rainbow-table attacks. bcrypt handles
salting internally — never salt manually on top of it.
"""
import bcrypt


def hash_password(plaintext: str) -> str:
    return bcrypt.hashpw(plaintext.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_password(plaintext: str, password_hash: str) -> bool:
    if not plaintext or not password_hash:
        return False
    try:
        return bcrypt.checkpw(plaintext.encode("utf-8"), password_hash.encode("utf-8"))
    except ValueError:
        # Malformed/legacy hash — never crash auth over a bad stored value.
        return False
