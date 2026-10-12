"""Encryption at rest for customer secrets (Kalshi private keys, etc.).

Uses ``MultiFernet`` (AES-128-CBC + HMAC-SHA256, authenticated) so keys can be
rotated: the first key in ENCRYPTION_KEYS encrypts, every key can decrypt.
Plaintext never leaves this module except to the in-memory signing code.
"""

from __future__ import annotations

from cryptography.fernet import Fernet, InvalidToken, MultiFernet

from kalshi_ai.config import Settings, get_settings


class EncryptionError(Exception):
    """Raised for any encryption/decryption failure. Never contains plaintext."""


class SecretCipher:
    def __init__(self, keys: list[str]):
        if not keys:
            raise EncryptionError("No encryption keys configured (ENCRYPTION_KEYS).")
        try:
            self._fernet = MultiFernet([Fernet(k.encode()) for k in keys])
        except (ValueError, TypeError) as exc:
            raise EncryptionError("Invalid Fernet key in ENCRYPTION_KEYS.") from exc

    @classmethod
    def from_settings(cls, settings: Settings | None = None) -> SecretCipher:
        settings = settings or get_settings()
        raw = settings.encryption_keys.get_secret_value()
        return cls([k.strip() for k in raw.split(",") if k.strip()])

    def encrypt(self, plaintext: str | bytes) -> bytes:
        data = plaintext.encode() if isinstance(plaintext, str) else plaintext
        return self._fernet.encrypt(data)

    def decrypt(self, token: bytes) -> bytes:
        try:
            return self._fernet.decrypt(token)
        except InvalidToken as exc:
            raise EncryptionError("Unable to decrypt secret (wrong key or tampered data).") from exc

    def rotate(self, token: bytes) -> bytes:
        """Re-encrypt a token under the current primary key."""
        try:
            return self._fernet.rotate(token)
        except InvalidToken as exc:
            raise EncryptionError("Unable to rotate secret.") from exc


def generate_key() -> str:
    return Fernet.generate_key().decode()
