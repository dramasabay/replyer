"""
Encrypts Telethon session strings before they touch the database.
A stolen DB row without the key file is useless.

SETUP:
    Run once: python -c "from crypto_utils import generate_key; generate_key()"
    This creates secret.key next to this file — back it up, never commit it,
    never expose it via the web server's static/public paths.
"""
from cryptography.fernet import Fernet
import os

# Same DATA_DIR convention as db.py — keeps the key alongside app.db on a
# mounted volume in Docker, falls back to this file's directory otherwise.
DATA_DIR = os.environ.get("DATA_DIR", os.path.dirname(__file__))
KEY_PATH = os.path.join(DATA_DIR, "secret.key")


def generate_key():
    if os.path.exists(KEY_PATH):
        raise RuntimeError(f"{KEY_PATH} already exists — refusing to overwrite.")
    key = Fernet.generate_key()
    with open(KEY_PATH, "wb") as f:
        f.write(key)
    os.chmod(KEY_PATH, 0o600)
    print(f"Key written to {KEY_PATH}. Back this up somewhere safe.")


def _load_key():
    if not os.path.exists(KEY_PATH):
        # auto-generate on first run so local dev "just works"
        generate_key()
    try:
        with open(KEY_PATH, "rb") as f:
            return f.read()
    except PermissionError as e:
        raise PermissionError(
            f"Cannot read encryption key at {KEY_PATH}: permission denied. "
            f"If running in Docker, ensure the file is owned by the container user (UID 1000)."
        ) from e


_fernet = None


def get_fernet():
    global _fernet
    if _fernet is None:
        _fernet = Fernet(_load_key())
    return _fernet


def encrypt(plaintext: str) -> bytes:
    return get_fernet().encrypt(plaintext.encode())


def decrypt(ciphertext: bytes) -> str:
    return get_fernet().decrypt(ciphertext).decode()
