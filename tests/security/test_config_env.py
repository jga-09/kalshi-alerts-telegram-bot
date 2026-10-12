"""Env-file values that are really comments must be treated as unset."""

from kalshi_ai.config import Settings


def test_comment_values_are_ignored(monkeypatch) -> None:
    monkeypatch.setenv("TELEGRAM_WEBHOOK_URL", "# optional: https://<DOMAIN>/telegram/webhook")
    monkeypatch.setenv("LOG_LEVEL", "   # note")
    s = Settings(_env_file=None)
    assert s.telegram_webhook_url == "" and s.log_level == "INFO"
