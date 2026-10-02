"use client";

import { useCallback, useEffect, useState } from "react";
import TelegramLogin from "@/components/TelegramLogin";
import { api, ApiError, Me, post } from "@/lib/api";

type PlanInfo = {
  plan: string;
  description: string;
  features: string[];
  prices: Record<string, { amount_cents: number; currency: string } | null>;
};

export default function BillingPage() {
  const [me, setMe] = useState<Me | null>(null);
  const [plans, setPlans] = useState<PlanInfo[]>([]);
  const [needLogin, setNeedLogin] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(() => {
    api<Me>("/api/auth/me")
      .then((m) => { setMe(m); setNeedLogin(false); })
      .catch((e) => (e instanceof ApiError && e.status === 401 ? setNeedLogin(true) : setError(e.message)));
    api<PlanInfo[]>("/api/billing/plans").then(setPlans).catch(() => undefined);
  }, []);
  useEffect(load, [load]);

  async function checkout(plan: string, interval: string) {
    try {
      const { url } = await post<{ url: string }>("/api/billing/checkout", { plan, interval });
      window.location.href = url; // Stripe-hosted checkout; access is granted only by the verified webhook
    } catch (e) {
      setError((e as Error).message);
    }
  }

  async function portal() {
    try {
      const { url } = await post<{ url: string }>("/api/billing/portal");
      window.location.href = url;
    } catch (e) {
      setError((e as Error).message);
    }
  }

  if (needLogin) return <div className="card space-y-3"><p>Log in with Telegram to manage billing.</p><TelegramLogin onLogin={load} /></div>;
  return (
    <div className="space-y-6">
      <h1 className="text-2xl font-semibold">Billing</h1>
      {error && <p className="text-sm text-red-400">{error}</p>}
      {me && (
        <div className="card flex items-center justify-between">
          <p>
            {me.subscription.active
              ? <>Active plan <b>{me.subscription.plan?.toUpperCase()}</b> until {me.subscription.expires_at?.slice(0, 10)}</>
              : "No active subscription"}
          </p>
          <button className="btn" onClick={portal}>Manage billing</button>
        </div>
      )}
      <div className="grid gap-4 md:grid-cols-2">
        {plans.map((p) => (
          <div key={p.plan} className="card space-y-3">
            <h2 className="text-lg font-semibold">{p.plan.toUpperCase()}</h2>
            <p className="text-sm text-slate-300">{p.description}</p>
            <div className="flex gap-2">
              {(["monthly", "yearly"] as const).map((interval) => {
                const price = p.prices[interval];
                return (
                  <button key={interval} className="btn" disabled={!price} onClick={() => checkout(p.plan, interval)}>
                    {interval} {price ? `$${(price.amount_cents / 100).toFixed(2)}` : "(unavailable)"}
                  </button>
                );
              })}
            </div>
          </div>
        ))}
      </div>
      <p className="text-xs text-slate-500">Subscriptions renew automatically until cancelled. See subscription terms.</p>
    </div>
  );
}
