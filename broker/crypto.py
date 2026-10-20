"""Encryption at rest, under CREDENTIAL_ENCRYPTION_KEY, for secrets the broker has to use as they are (not hash)."""

from cryptography.fernet import Fernet
from flask import current_app


def encrypt(plaintext: str) -> str:
    """Encrypt a secret, such as a third-party password, for keeping at rest."""
    return _fernet().encrypt(plaintext.encode()).decode()


def decrypt(token: str) -> str:
    """Return the secret that encrypt turned into `token`."""
    return _fernet().decrypt(token.encode()).decode()


def _fernet() -> Fernet:
    return Fernet(current_app.config["CREDENTIAL_ENCRYPTION_KEY"])
