"use client";

import { Suspense, useEffect, useState } from "react";
import { useSearchParams } from "next/navigation";
import { api, post } from "@/lib/api";

type Info = {
  valid: boolean;
  instructions: string[];
  environments: string[];
  default_environment: string;
  already_connected: boolean;
  accepted_key_types: string[];
};

type Result = { status: string; environment: string; key_type: string; api_key_id_hint: string; can_trade: boolean };

function ConnectForm() {
  const token = useSearchParams().get("token") ?? "";
  const [info, setInfo] = useState<Info | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [keyId, setKeyId] = useState("");
  const [pem, setPem] = useState("");
  const [env, setEnv] = useState("demo");
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<Result | null>(null);

  useEffect(() => {
    if (!token) {
      setError("Missing connect link. Send /connect to the Kalshi AI bot in Telegram.");
      return;
    }
    api<Info>(`/api/kalshi/connect?token=${encodeURIComponent(token)}`)
      .then((i) => {
        setInfo(i);
        setEnv(i.default_environment);
      })
      .catch((e) => setError(e.message));
  }, [token]);

  async function onFile(file: File | undefined) {
    if (!file) return;
    if (file.size > 16_384) {
      setError("That file is too large to be a private key.");
      return;
    }
    setPem(await file.text()); // read locally; never stored in browser storage
  }

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      const r = await post<Result>("/api/kalshi/connect", {
        token,
        api_key_id: keyId.trim(),
        private_key_pem: pem,
        environment: env,
      });
      setResult(r);
      setPem("");
      setKeyId("");
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setBusy(false);
    }
  }

  if (result) {
    return (
      <div className="card space-y-2">
        <h1 className="text-xl font-semibold text-emerald-400">Kalshi connected</h1>
        <p>Key {result.api_key_id_hint} ({result.key_type.toUpperCase()}) verified on {result.environment}.</p>
        <p>{result.can_trade ? "This key can place trades if you later enable live trading." : "This key is read-only."}</p>
        <p className="text-sm text-slate-400">You can close this page and return to Telegram.</p>
      </div>
    );
  }

  return (
    <div className="mx-auto max-w-2xl space-y-6">
      <h1 className="text-2xl font-semibold">Connect your Kalshi account</h1>
      {error && <p className="rounded-lg border border-red-800 bg-red-950/50 p-3 text-sm text-red-300">{error}</p>}
      {info && (
        <>
          <ol className="card list-decimal space-y-1 pl-8 text-sm text-slate-300">
            {info.instructions.map((s) => <li key={s}>{s}</li>)}
          </ol>
          {info.already_connected && (
            <p className="text-sm text-amber-400">An existing connection will be replaced.</p>
          )}
          <form onSubmit={submit} className="card space-y-4" autoComplete="off">
            <div>
              <label className="label" htmlFor="env">Environment</label>
              <select id="env" className="input" value={env} onChange={(e) => setEnv(e.target.value)}>
                {info.environments.map((x) => <option key={x} value={x}>{x === "prod" ? "Production (real money)" : "Demo"}</option>)}
              </select>
            </div>
            <div>
              <label className="label" htmlFor="kid">API Key ID</label>
              <input id="kid" className="input" value={keyId} onChange={(e) => setKeyId(e.target.value)} required
                     spellCheck={false} autoComplete="off" />
            </div>
            <div>
              <label className="label" htmlFor="pem">Private key (.pem) - {info.accepted_key_types.join(" or ")}</label>
              <input type="file" accept=".pem,.key,.txt" className="mb-2 text-sm" onChange={(e) => onFile(e.target.files?.[0])} />
              <textarea id="pem" className="input h-40 font-mono text-xs" value={pem} onChange={(e) => setPem(e.target.value)}
                        placeholder="-----BEGIN PRIVATE KEY-----" required spellCheck={false} autoComplete="off" />
            </div>
            <p className="text-xs text-slate-400">
              Sent over HTTPS, verified with Kalshi, then encrypted at rest. Never share this key in Telegram or email.
              Kalshi AI never asks for your Kalshi password.
            </p>
            <button className="btn" disabled={busy}>{busy ? "Verifying with Kalshi..." : "Verify & connect"}</button>
          </form>
        </>
      )}
    </div>
  );
}

export default function ConnectPage() {
  return (
    <Suspense fallback={<p>Loading...</p>}>
      <ConnectForm />
    </Suspense>
  );
}
