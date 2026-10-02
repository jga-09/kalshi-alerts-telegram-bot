# Legal & compliance items — review with qualified counsel BEFORE launch

This is an engineering checklist of issues that **may** apply. It is not legal advice. Have a qualified
attorney and compliance professional (with CFTC/derivatives, securities, consumer-protection and privacy
experience) review the product, marketing and disclosures for every jurisdiction you serve.

## Regulatory status of an automated trading service

* **CFTC / Commodity Exchange Act.** Kalshi is a CFTC-designated contract market. A service that, for
  compensation, provides trading advice or directs trading in customers' event-contract accounts may implicate
  registration categories such as Commodity Trading Advisor (CTA) or other intermediary roles, or require an
  exemption. Determine whether signals, auto-trading, or both trigger registration, NFA membership, disclosure
  documents, recordkeeping and performance-reporting rules.
* **Securities / investment-adviser law.** Assess whether any service element could be treated as investment
  advice under federal or state law.
* **Kalshi's terms.** Confirm that Kalshi's Member Agreement, API terms and developer policies permit a
  third-party commercial service to place orders with customer-created API keys, any limits on automated
  trading, rate limits, data redistribution (showing Kalshi prices to non-members), and branding/trademark use.
* **Eligibility & geography.** Kalshi restricts users by jurisdiction and some states challenge event contracts.
  Decide whether to geo-restrict subscribers and how to verify.

## Marketing & disclosures

* No guaranteed returns or "risk-free" claims; hypothetical/paper/backtest performance needs prominent
  disclaimers (and may be restricted for registrants).
* Risk Disclosure, Terms, Privacy Policy and Subscription Terms in `shared/kalshi_ai/domain/disclosures.py` are
  **templates** — replace via the `disclosures` table after legal review; bump `CURRENT_DISCLOSURE_VERSION` to
  force re-acceptance.
* AI-generated content disclosures (some jurisdictions regulate automated decision-making and AI marketing).

## Consumer protection & payments

* Automatic-renewal laws (clear terms, affirmative consent, easy cancellation, renewal reminders for annual plans).
* Refund policy consistent with Stripe settings and local law; chargeback handling.
* Stripe's restricted-business policies for trading/financial-signal products.

## Privacy & data

* Privacy policy covering Telegram IDs, payment metadata, Kalshi connection metadata, logs; data-retention
  schedule; deletion requests (GDPR/CCPA-style rights if applicable).
* Data-source licences: NewsAPI, X, Reddit, Coinbase, Deribit, FRED and any gold/futures vendor — confirm that
  commercial use and redistribution of derived signals are permitted.
* Telegram Bot Platform terms.

## Operations

* Recordkeeping: audit logs, orders and signals are retained in the database — confirm required retention periods.
* Incident response and customer notification procedures for security incidents or erroneous trades.
* Error-trade policy: who bears losses from software or data errors; reflect it in the Terms.
* Tax reporting obligations, if any, for subscription revenue in each jurisdiction.

**Do not enable `LIVE_TRADING=true` for real customers until this review is complete.**
