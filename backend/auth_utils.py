"""
Authentication and Authorization Security Utilities.
Provides tamper-proof, encrypted Bearer access tokens using AES-128-CBC + HMAC-SHA256
via cryptography.fernet.
"""
import json
import time
try:
    import crypto_utils
except ImportError:
    from backend import crypto_utils

TOKEN_EXPIRE_SECONDS = 30 * 24 * 3600  # 30 days


def create_access_token(user_id: int, phone: str, is_admin: bool = False) -> str:
    """Generate an encrypted, tamper-proof session token."""
    payload = {
        "user_id": user_id,
        "phone": phone,
        "is_admin": bool(is_admin),
        "created_at": time.time(),
        "exp": time.time() + TOKEN_EXPIRE_SECONDS,
    }
    json_bytes = json.dumps(payload).encode("utf-8")
    encrypted_token = crypto_utils.get_fernet().encrypt(json_bytes)
    return encrypted_token.decode("utf-8")


def verify_access_token(token: str) -> dict:
    """Decrypt and validate the token. Returns payload dict or raises ValueError."""
    if not token:
        raise ValueError("Token is missing")
    try:
        decrypted_bytes = crypto_utils.get_fernet().decrypt(token.encode("utf-8"))
        payload = json.loads(decrypted_bytes.decode("utf-8"))
    except Exception as e:
        raise ValueError(f"Invalid or tampered token: {e}")

    if payload.get("exp") and time.time() > payload["exp"]:
        raise ValueError("Token has expired. Please log in again.")

    return payload
