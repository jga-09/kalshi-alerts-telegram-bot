import Link from "next/link";

const steps = [
  "Activate a subscription (Stripe or access code)",
  "Connect your Kalshi account with a trade-only API key",
  "Choose a risk level",
  "Start paper trading",
  "Review performance - P&L and calibration, not just win rate",
  "Enable live trading only if you want to, with explicit confirmation",
];

export default function Home() {
  const bot = process.env.NEXT_PUBLIC_TELEGRAM_BOT_USERNAME ?? "KalshiAIBot";
  return (
    <div className="space-y-8">
      <section className="space-y-3">
        <h1 className="text-3xl font-bold">Kalshi AI</h1>
        <p className="max-w-2xl text-slate-300">
          Structured, evidence-based analysis of Kalshi markets delivered in Telegram. Every probability is a model
          estimate with its supporting and conflicting signals - never a guarantee.
        </p>
        <a className="btn inline-block" href={`https://t.me/${bot}`}>Open in Telegram</a>
      </section>
      <section className="card">
        <h2 className="mb-3 font-semibold">How it works</h2>
        <ol className="list-decimal space-y-1 pl-5 text-slate-300">
          {steps.map((s) => <li key={s}>{s}</li>)}
        </ol>
      </section>
      <section className="card border-amber-700/50">
        <h2 className="mb-2 font-semibold text-amber-400">Important</h2>
        <p className="text-sm text-slate-300">
          Paper trading is the default. Live trading is always opt-in, requires a separate confirmation of your risk
          limits, and every order must pass a deterministic risk engine. You can stop everything instantly with the
          emergency stop. <Link className="underline" href="/legal/risk">Read the risk disclosure.</Link>
        </p>
      </section>
    </div>
  );
}
