from cryptography.fernet import Fernet
from flask import current_app


def encrypt(plaintext: str) -> str:
    """Encrypts a secret (e.g. a stored third-party password) so it isn't kept in
    plaintext at rest. Reversible, unlike a password hash, because the broker has
    to hand the real value back to whoever needs to authenticate with it."""
    return _fernet().encrypt(plaintext.encode()).decode()


def decrypt(token: str) -> str:
    return _fernet().decrypt(token.encode()).decode()


def _fernet() -> Fernet:
    return Fernet(current_app.config["CREDENTIAL_ENCRYPTION_KEY"])
