"""Default legal/risk disclosure text. Override per deployment via the `disclosures` table.

THESE ARE TEMPLATES. They must be reviewed by a qualified attorney before production use.
"""

from __future__ import annotations

CURRENT_DISCLOSURE_VERSION = "2026-10-01"
DISCLOSURE_SLUGS = ("terms", "privacy", "risk", "subscription")

SHORT_RISK_NOTICE = (
    "⚠️ AI probabilities are estimates, not guarantees. Trading involves risk and you can lose money. "
    "Past performance does not guarantee future results."
)

DEFAULT_DISCLOSURES: dict[str, dict[str, str]] = {
    "risk": {
        "title": "Risk Disclosure",
        "body_markdown": """# Risk Disclosure

- **AI predictions are estimates.** Probabilities shown by Kalshi AI are model estimates that can be wrong.
  They are never guarantees of any outcome.
- **Trading involves risk.** Event contracts can lose their entire value. Only trade money you can afford to lose.
- **Past performance does not guarantee future results.** Backtests and paper-trading results are hypothetical
  and do not reflect real fills, fees, slippage or liquidity.
- **Automated trading can result in losses.** Software, data feeds, exchanges and networks can fail.
  Risk limits reduce but do not eliminate losses. "Low" risk mode does not mean safe.
- **You control your Kalshi account.** Kalshi AI only acts through the API key you create and can revoke at any
  time on kalshi.com. Review the permissions (scopes) you grant. Never grant transfer/withdrawal scopes.
- Kalshi AI is not a broker, investment adviser or fiduciary and does not provide personalized investment advice.
""",
    },
    "terms": {
        "title": "Terms of Service",
        "body_markdown": """# Terms of Service (template - requires legal review)

1. Eligibility: you must be legally permitted to trade on Kalshi in your jurisdiction and comply with Kalshi's
   member agreement.
2. The service provides software tools and model-generated estimates for informational purposes.
3. You are solely responsible for your trading decisions, including any automated trading you enable.
4. No guarantee of profits, accuracy, availability or fitness for a particular purpose.
5. We may suspend accounts for abuse, non-payment, or to protect users and the platform.
""",
    },
    "privacy": {
        "title": "Privacy Policy",
        "body_markdown": """# Privacy Policy (template - requires legal review)

- We store your Telegram user ID, username, subscription records and trading settings.
- Kalshi API credentials are encrypted at rest and are never shown, logged or sent through Telegram.
- Payment card data is handled by Stripe; we never receive card numbers.
- You can disconnect Kalshi at any time; encrypted credentials are deleted on disconnect.
- Audit logs of security-relevant actions are retained for compliance and fraud prevention.
""",
    },
    "subscription": {
        "title": "Subscription Terms",
        "body_markdown": """# Subscription Terms (template - requires legal review)

- Subscriptions renew automatically each month or year until cancelled.
- You can cancel any time in the billing portal; access continues until the end of the paid period.
- Failed payments may suspend premium features. Refunds follow the published refund policy; a full refund ends access.
- Access codes grant time-limited access and cannot be exchanged for cash.
""",
    },
}
