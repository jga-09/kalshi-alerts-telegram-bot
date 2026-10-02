"""KalshiAuthService - request signing per Kalshi's official API-key scheme.

Verified against Kalshi's official OpenAPI-generated SDK (kalshi_python 3.31.0,
``auth.py``) and official starter code:

* Headers: ``KALSHI-ACCESS-KEY``, ``KALSHI-ACCESS-TIMESTAMP`` (ms), ``KALSHI-ACCESS-SIGNATURE``
* Signed message: ``timestamp_ms + METHOD + path`` where path is the full URL path
  (e.g. ``/trade-api/v2/portfolio/balance``) WITHOUT the query string.
* RSA keys: RSA-PSS, MGF1(SHA-256), salt length = digest length, SHA-256.
* Ed25519 keys: Ed25519 signature over the same message.
* Signature is base64-encoded.

Kalshi does not offer OAuth for third-party apps; each customer creates their own
API key on kalshi.com and supplies the key ID + private key to us over HTTPS.
"""

from __future__ import annotations

import base64
import time
from dataclasses import dataclass
from typing import Literal

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPrivateKey

from kalshi_ai.kalshi.errors import KalshiCredentialFormatError

KeyType = Literal["rsa", "ed25519"]
MAX_PEM_BYTES = 16_384


@dataclass(frozen=True)
class LoadedKey:
    key: RSAPrivateKey | Ed25519PrivateKey
    key_type: KeyType
    public_fingerprint: str  # sha256 of DER SubjectPublicKeyInfo, hex


def load_private_key(pem: str | bytes) -> LoadedKey:
    data = pem.encode() if isinstance(pem, str) else pem
    if len(data) > MAX_PEM_BYTES:
        raise KalshiCredentialFormatError("Private key file is too large.")
    if b"PRIVATE KEY-----" not in data:
        raise KalshiCredentialFormatError("Expected a PEM-encoded private key.")
    try:
        key = serialization.load_pem_private_key(data, password=None)
    except (ValueError, TypeError):
        # Do not chain the original message: it can echo key material in some backends.
        raise KalshiCredentialFormatError("Could not parse private key (encrypted keys are not supported).") from None
    if isinstance(key, RSAPrivateKey):
        if key.key_size < 2048:
            raise KalshiCredentialFormatError("RSA key must be at least 2048 bits.")
        key_type: KeyType = "rsa"
    elif isinstance(key, Ed25519PrivateKey):
        key_type = "ed25519"
    else:
        raise KalshiCredentialFormatError("Private key must be an RSA or Ed25519 key.")
    der = key.public_key().public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
    digest = hashes.Hash(hashes.SHA256())
    digest.update(der)
    return LoadedKey(key=key, key_type=key_type, public_fingerprint=digest.finalize().hex())


class KalshiAuthService:
    """Signs requests for ONE customer's credential. Holds the key only in memory."""

    def __init__(self, api_key_id: str, private_key_pem: str | bytes):
        if not api_key_id or len(api_key_id) > 128:
            raise KalshiCredentialFormatError("Invalid API key ID.")
        self._api_key_id = api_key_id
        self._loaded = load_private_key(private_key_pem)

    def __repr__(self) -> str:  # never leak key material via repr
        return f"KalshiAuthService(key_type={self._loaded.key_type!r}, key_id=***{self._api_key_id[-4:]})"

    @property
    def key_type(self) -> KeyType:
        return self._loaded.key_type

    @property
    def public_fingerprint(self) -> str:
        return self._loaded.public_fingerprint

    @property
    def api_key_id(self) -> str:
        return self._api_key_id

    def sign(self, message: bytes) -> bytes:
        key = self._loaded.key
        if isinstance(key, Ed25519PrivateKey):
            return key.sign(message)
        return key.sign(
            message,
            padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
            hashes.SHA256(),
        )

    def headers(self, method: str, path: str, timestamp_ms: int | None = None) -> dict[str, str]:
        ts = str(timestamp_ms if timestamp_ms is not None else int(time.time() * 1000))
        path_only = path.split("?", 1)[0]
        message = f"{ts}{method.upper()}{path_only}".encode()
        signature = base64.b64encode(self.sign(message)).decode()
        return {
            "KALSHI-ACCESS-KEY": self._api_key_id,
            "KALSHI-ACCESS-TIMESTAMP": ts,
            "KALSHI-ACCESS-SIGNATURE": signature,
        }
