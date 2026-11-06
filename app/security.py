"""Passwords, sessions and encryption of users' API keys.

SECRET_KEY signs session cookies and derives the key that encrypts stored API
keys. Set it as an env var; if missing, one is generated once and kept on the
data volume (losing it means users must sign in again and re-enter API keys).
"""
import base64
import hashlib
import hmac
import os
import secrets
import time

from cryptography.fernet import Fernet, InvalidToken

from . import config

SESSION_DAYS = 30


def _secret() -> bytes:
    env = os.getenv("SECRET_KEY", "").strip()
    if env:
        return env.encode()
    path = config.DATA_DIR / "secret.key"
    if not path.exists():
        path.write_text(secrets.token_urlsafe(48))
        path.chmod(0o600)
    return path.read_text().strip().encode()


SECRET = _secret()
_fernet = Fernet(base64.urlsafe_b64encode(hashlib.sha256(SECRET + b"|api-keys").digest()))


# ---------------------------------------------------------------- passwords

def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.scrypt(password.encode(), salt=salt, n=2 ** 14, r=8, p=1, dklen=32)
    return f"scrypt${salt.hex()}${dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        _, salt, digest = stored.split("$")
        dk = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=2 ** 14, r=8, p=1, dklen=32)
        return hmac.compare_digest(dk.hex(), digest)
    except Exception:
        return False


# ---------------------------------------------------------------- sessions

def make_session(user_id: int) -> str:
    exp = int(time.time()) + SESSION_DAYS * 86400
    body = f"{user_id}.{exp}"
    sig = hmac.new(SECRET, body.encode(), hashlib.sha256).hexdigest()
    return f"{body}.{sig}"


def read_session(token: str | None) -> int | None:
    if not token:
        return None
    try:
        uid, exp, sig = token.split(".")
        good = hmac.new(SECRET, f"{uid}.{exp}".encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig, good) or int(exp) < time.time():
            return None
        return int(uid)
    except Exception:
        return None


# ---------------------------------------------------------------- API keys at rest

def encrypt(value: str) -> str:
    return _fernet.encrypt(value.encode()).decode()


def decrypt(value: str) -> str | None:
    try:
        return _fernet.decrypt(value.encode()).decode()
    except InvalidToken:
        return None
