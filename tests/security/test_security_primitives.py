from __future__ import annotations

import hashlib
import hmac
import time

import pytest
from cryptography.fernet import Fernet

from kalshi_ai.logging import REDACTED, redact_mapping, redact_value
from kalshi_ai.security.crypto import EncryptionError, SecretCipher
from kalshi_ai.security.hashing import generate_activation_code, hash_code, normalize_code
from kalshi_ai.security.telegram_auth import TelegramAuthError, verify_login_widget
from kalshi_ai.security.tokens import TokenError, create_access_token, decode_access_token


def test_cipher_roundtrip_and_ciphertext_hides_plaintext() -> None:
    cipher = SecretCipher([Fernet.generate_key().decode()])
    token = cipher.encrypt("-----BEGIN PRIVATE KEY-----secret")
    assert b"secret" not in token
    assert cipher.decrypt(token) == b"-----BEGIN PRIVATE KEY-----secret"


def test_cipher_key_rotation() -> None:
    old, new = Fernet.generate_key().decode(), Fernet.generate_key().decode()
    token = SecretCipher([old]).encrypt("value")
    rotated_cipher = SecretCipher([new, old])
    assert rotated_cipher.decrypt(token) == b"value"
    rotated = rotated_cipher.rotate(token)
    assert SecretCipher([new]).decrypt(rotated) == b"value"


def test_cipher_rejects_tampering_and_wrong_key() -> None:
    cipher = SecretCipher([Fernet.generate_key().decode()])
    token = bytearray(cipher.encrypt("value"))
    token[-5] ^= 1
    with pytest.raises(EncryptionError):
        cipher.decrypt(bytes(token))
    with pytest.raises(EncryptionError):
        SecretCipher([Fernet.generate_key().decode()]).decrypt(cipher.encrypt("x"))


def test_cipher_requires_keys() -> None:
    with pytest.raises(EncryptionError):
        SecretCipher([])


def test_activation_codes_are_random_and_hash_is_keyed() -> None:
    codes = {generate_activation_code() for _ in range(200)}
    assert len(codes) == 200
    code = next(iter(codes))
    assert code.startswith("KAI-") and len(normalize_code(code)) == 19
    assert hash_code(code, "pepper-a") != hash_code(code, "pepper-b")
    # Case/formatting-insensitive
    assert hash_code(code.lower().replace("-", " "), "p") == hash_code(code, "p")


def test_redaction_of_keys_and_values() -> None:
    data = {
        "api_key": "abc",
        "private_key_pem": "x",
        "nested": {"Authorization": "Bearer abcdefghijkl", "ok": 1},
        "msg": "token 123456789:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA and sk_live_abcdefghijk",
        "pem": "-----BEGIN RSA PRIVATE KEY-----\nMIIE\n-----END RSA PRIVATE KEY-----",
    }
    out = redact_mapping(data)
    assert out["api_key"] == REDACTED
    assert out["private_key_pem"] == REDACTED
    assert out["nested"]["Authorization"] == REDACTED
    assert out["nested"]["ok"] == 1
    assert "AAAAAAAA" not in out["msg"] and "sk_live" not in out["msg"]
    assert redact_value("-----BEGIN PRIVATE KEY-----abc-----END PRIVATE KEY-----") == REDACTED


def test_jwt_roundtrip_and_tamper() -> None:
    token = create_access_token("user-1", "customer")
    claims = decode_access_token(token)
    assert claims["sub"] == "user-1" and claims["role"] == "customer"
    with pytest.raises(TokenError):
        decode_access_token(token[:-2] + ("A" if token[-1] != "A" else "B") + token[-1])
    with pytest.raises(TokenError):
        decode_access_token(create_access_token("u", "customer", ttl_minutes=-1))


def _signed(data: dict, bot_token: str) -> dict:
    check = "\n".join(f"{k}={data[k]}" for k in sorted(data))
    secret = hashlib.sha256(bot_token.encode()).digest()
    return {**data, "hash": hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()}


def test_telegram_login_verification() -> None:
    bot = "123:abc"
    payload = _signed({"id": 42, "first_name": "A", "auth_date": int(time.time())}, bot)
    assert verify_login_widget(payload, bot)["id"] == 42
    with pytest.raises(TelegramAuthError):
        verify_login_widget({**payload, "id": 43}, bot)  # tampered
    with pytest.raises(TelegramAuthError):
        verify_login_widget(payload, "other-token")
    old = _signed({"id": 42, "auth_date": int(time.time()) - 200_000}, bot)
    with pytest.raises(TelegramAuthError):
        verify_login_widget(old, bot)


def test_production_rejects_weak_config(monkeypatch: pytest.MonkeyPatch) -> None:
    from kalshi_ai.config import Settings

    with pytest.raises(ValueError, match="Insecure production configuration"):
        Settings(app_env="production", jwt_secret="dev-only-short", encryption_keys="")
