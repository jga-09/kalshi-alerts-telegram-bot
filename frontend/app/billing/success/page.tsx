export default function BillingSuccess() {
  return (
    <div className="card space-y-2">
      <h1 className="text-xl font-semibold">Thanks - payment received by Stripe</h1>
      <p className="text-slate-300">
        Your subscription activates as soon as Stripe confirms the payment to our servers (usually within seconds).
        Return to Telegram and send /subscription to check.
      </p>
    </div>
  );
}
