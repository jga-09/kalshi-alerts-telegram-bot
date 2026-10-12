"""Print fresh secrets for .env. Needs only the `cryptography` package.

python3 scripts/gen_secrets.py >> .env
"""

import secrets

from cryptography.fernet import Fernet

print("# Generated secrets - never commit")
print(f"JWT_SECRET={secrets.token_urlsafe(48)}")
print(f"ENCRYPTION_KEYS={Fernet.generate_key().decode()}")
print(f"CODE_HASH_PEPPER={secrets.token_urlsafe(32)}")
print(f"INTERNAL_API_TOKEN={secrets.token_urlsafe(32)}")
print(f"TELEGRAM_WEBHOOK_SECRET={secrets.token_urlsafe(32)}")
