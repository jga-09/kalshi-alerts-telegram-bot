# Stripe

Implemented with **stripe-python 16** (`StripeClient.v1.*`, async methods; API version pinned by the SDK,
`2026-09-30.endive` at build time). Code: `shared/kalshi_ai/payments/stripe_service.py`.

## Setup

1. Create one Product per plan (SIGNALS, PRO, AUTO, PREMIUM) with a monthly and a yearly recurring Price.
2. Put the Price IDs in `STRIPE_PRICE_<PLAN>_<MONTHLY|YEARLY>` **or** manage them in the database
   (`PUT /api/admin/plans` or `python scripts/manage.py seed-plans --amounts pro_monthly=4900,...`).
   Display amounts come from the `subscription_plans` table — nothing is hardcoded.
3. Optional free trial: `STRIPE_TRIAL_DAYS=7` (applied via `subscription_data.trial_period_days`).
4. Webhook endpoint `https://<domain>/api/stripe/webhook` with events listed in DEPLOYMENT.md; copy the signing
   secret into `STRIPE_WEBHOOK_SECRET`.
5. Enable the Customer Portal (Settings → Billing → Customer portal) for self-service cancel/reactivate.

## Trust model

* The frontend/bot only ever receive a **Checkout URL**. Access is granted exclusively from webhook events whose
  `Stripe-Signature` HMAC verifies (5-minute tolerance against replays).
* Each event ID is stored in `stripe_events`; duplicates return `duplicate` and change nothing.
* A redirect to `/billing/success` grants nothing.

## Lifecycle mapping

| Stripe | Kalshi AI |
|---|---|
| `checkout.session.completed` | link Stripe customer ↔ user (`client_reference_id`) |
| `customer.subscription.created/updated/resumed` | upsert subscription: plan from price ID, status, `expires_at` = item `current_period_end` (basil+ API) or legacy field, cancel-at-period-end |
| `trialing` / `active` / `past_due` | access granted until `expires_at` |
| `incomplete` | no access |
| `customer.subscription.deleted` / `canceled` | status canceled, access ends at `ended_at` |
| `invoice.paid` / `invoice.payment_succeeded` | payment PAID, past_due → active, extend to the invoice period end |
| `invoice.payment_failed` | payment FAILED, active → past_due |
| `charge.refunded` (full) | status REFUNDED, access revoked immediately; later events cannot revive it |
| `charge.refunded` (partial) | payment marked refunded, access unchanged |

Whenever access drops below AUTO, auto and live trading are switched off automatically. A periodic job also
expires any lapsed subscription (`expire_subscriptions`, every 5 minutes).

Subscriptions from access codes and admin grants coexist with Stripe subscriptions; the highest active plan wins.

## Local testing

```bash
stripe listen --forward-to localhost:8000/api/stripe/webhook
stripe trigger customer.subscription.created
```
