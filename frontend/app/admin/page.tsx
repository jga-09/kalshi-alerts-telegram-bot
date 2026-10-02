"use client";

import { useCallback, useEffect, useState } from "react";
import TelegramLogin from "@/components/TelegramLogin";
import { api, ApiError, Me, post } from "@/lib/api";

type Json = Record<string, unknown>;
const TABS = ["Overview", "Performance", "Users", "Codes", "Trades", "Logs", "Kill switch"] as const;
type Tab = (typeof TABS)[number];

function Stat({ label, value }: { label: string; value: unknown }) {
  return (
    <div className="card">
      <div className="label">{label}</div>
      <div className="text-2xl font-semibold">{String(value ?? "-")}</div>
    </div>
  );
}

function Table({ rows, columns }: { rows: Json[]; columns: string[] }) {
  return (
    <div className="overflow-x-auto rounded-xl border border-slate-800">
      <table className="w-full text-left text-sm">
        <thead className="bg-slate-900 text-xs uppercase text-slate-400">
          <tr>{columns.map((c) => <th key={c} className="px-3 py-2">{c}</th>)}</tr>
        </thead>
        <tbody>
          {rows.map((r, i) => (
            <tr key={i} className="border-t border-slate-800">
              {columns.map((c) => (
                <td key={c} className="max-w-xs truncate px-3 py-2">
                  {typeof r[c] === "object" ? JSON.stringify(r[c]) : String(r[c] ?? "")}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function useLoad<T>(path: string, deps: unknown[] = []) {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  const reload = useCallback(() => {
    api<T>(path).then(setData).catch((e) => setError(e.message));
  }, [path, ...deps]);
  useEffect(reload, [reload]);
  return { data, error, reload };
}

function Overview() {
  const stats = useLoad<Json>("/api/admin/stats");
  const sources = useLoad<Json[]>("/api/admin/health/sources");
  const events = useLoad<Json[]>("/api/admin/events?limit=20");
  const s = stats.data ?? {};
  return (
    <div className="space-y-6">
      <div className="grid gap-4 md:grid-cols-4">
        <Stat label="Total users" value={s.total_users} />
        <Stat label="Active subscriptions" value={s.active_subscriptions} />
        <Stat label="Expired subscriptions" value={s.expired_subscriptions} />
        <Stat label="Est. MRR (USD)" value={s.estimated_mrr_usd} />
        <Stat label="Auto traders" value={s.active_auto_traders} />
        <Stat label="Live traders" value={s.live_traders} />
        <Stat label="Paper traders" value={s.paper_traders} />
        <Stat label="Open orders" value={s.open_orders} />
        <Stat label="Global kill switch" value={s.global_kill_switch ? "ACTIVE" : "off"} />
        <Stat label="Live suspended (monitor)" value={s.live_trading_suspended_by_monitor ? "YES" : "no"} />
        <Stat label="Order errors 24h" value={(s.order_errors_24h as Json | undefined)?.errors} />
      </div>
      <h2 className="font-semibold">Data-source health</h2>
      <Table rows={sources.data ?? []} columns={["source", "status", "last_success_at", "latency_ms", "consecutive_failures", "last_error"]} />
      <h2 className="font-semibold">System events</h2>
      <Table rows={events.data ?? []} columns={["created_at", "severity", "type", "message"]} />
    </div>
  );
}

function Performance() {
  const perf = useLoad<Json>("/api/admin/performance?days=30");
  const models = useLoad<Json>("/api/admin/models?days=30");
  const paper = (perf.data?.paper ?? {}) as Json;
  const live = (perf.data?.live ?? {}) as Json;
  return (
    <div className="space-y-6">
      <p className="text-sm text-slate-400">Optimise for expected value and calibration - not win rate.</p>
      <div className="grid gap-4 md:grid-cols-4">
        <Stat label="Paper trades" value={paper.trades} />
        <Stat label="Paper P&L" value={paper.realized_pnl} />
        <Stat label="Paper win rate" value={paper.win_rate != null ? `${Math.round(Number(paper.win_rate) * 100)}%` : "-"} />
        <Stat label="Paper max drawdown" value={paper.max_drawdown} />
        <Stat label="Live trades" value={live.trades} />
        <Stat label="Live P&L" value={live.realized_pnl} />
        <Stat label="Model Brier (30d)" value={(models.data?.model_brier as number | undefined)?.toFixed?.(4)} />
        <Stat label="Market Brier (30d)" value={(models.data?.market_brier as number | undefined)?.toFixed?.(4)} />
      </div>
      <h2 className="font-semibold">Calibration (predicted vs observed)</h2>
      <Table rows={(models.data?.calibration as Json[]) ?? []} columns={["lower", "upper", "count", "mean_predicted", "observed_rate"]} />
      <h2 className="font-semibold">Paper performance by market</h2>
      <Table
        rows={Object.entries((perf.data?.paper_by_market as Record<string, Json>) ?? {}).map(([k, v]) => ({ market: k, ...v }))}
        columns={["market", "trades", "realized_pnl", "win_rate", "brier_score", "max_drawdown"]}
      />
    </div>
  );
}

function Users() {
  const users = useLoad<Json[]>("/api/admin/users?limit=200");
  const [msg, setMsg] = useState<string | null>(null);
  async function act(id: string, action: string) {
    try {
      if (action === "suspend") {
        const reason = prompt("Reason for suspension?");
        if (!reason) return;
        await post(`/api/admin/users/${id}/suspend`, { reason });
      } else if (action === "reactivate") await post(`/api/admin/users/${id}/reactivate`);
      else if (action === "extend") {
        const days = Number(prompt("Extend by how many days?", "30"));
        if (!days) return;
        await post(`/api/admin/users/${id}/extend`, { days });
      } else if (action === "block_auto") await post(`/api/admin/users/${id}/auto-trading`, { allowed: false });
      else if (action === "allow_auto") await post(`/api/admin/users/${id}/auto-trading`, { allowed: true });
      setMsg(`${action} OK`);
      users.reload();
    } catch (e) {
      setMsg((e as Error).message);
    }
  }
  return (
    <div className="space-y-3">
      {msg && <p className="text-sm text-slate-300">{msg}</p>}
      <div className="overflow-x-auto rounded-xl border border-slate-800">
        <table className="w-full text-sm">
          <thead className="bg-slate-900 text-xs uppercase text-slate-400">
            <tr><th className="px-3 py-2 text-left">User</th><th>Role</th><th>Status</th><th>Actions</th></tr>
          </thead>
          <tbody>
            {(users.data ?? []).map((u) => (
              <tr key={String(u.id)} className="border-t border-slate-800">
                <td className="px-3 py-2">@{String(u.telegram_username ?? "-")} <span className="text-slate-500">{String(u.telegram_user_id)}</span></td>
                <td className="text-center">{String(u.role)}</td>
                <td className="text-center">{String(u.status)}</td>
                <td className="space-x-2 py-2 text-center">
                  {u.status === "active"
                    ? <button className="text-red-400" onClick={() => act(String(u.id), "suspend")}>suspend</button>
                    : <button className="text-emerald-400" onClick={() => act(String(u.id), "reactivate")}>reactivate</button>}
                  <button onClick={() => act(String(u.id), "extend")}>extend</button>
                  <button onClick={() => act(String(u.id), "block_auto")}>disable auto</button>
                  <button onClick={() => act(String(u.id), "allow_auto")}>allow auto</button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

function Codes() {
  const codes = useLoad<Json[]>("/api/admin/codes");
  const [plan, setPlan] = useState("pro");
  const [days, setDays] = useState(30);
  const [count, setCount] = useState(1);
  const [maxUses, setMaxUses] = useState(1);
  const [generated, setGenerated] = useState<string[]>([]);
  const [error, setError] = useState<string | null>(null);
  async function generate() {
    try {
      const r = await post<{ codes: { code: string }[] }>("/api/admin/codes", { plan, duration_days: days, count, max_uses: maxUses });
      setGenerated(r.codes.map((c) => c.code));
      codes.reload();
    } catch (e) {
      setError((e as Error).message);
    }
  }
  return (
    <div className="space-y-4">
      <div className="card grid gap-3 md:grid-cols-5">
        <div><label className="label">Plan</label>
          <select className="input" value={plan} onChange={(e) => setPlan(e.target.value)}>
            {["signals", "pro", "auto", "premium"].map((p) => <option key={p}>{p}</option>)}
          </select></div>
        <div><label className="label">Duration (days)</label>
          <select className="input" value={days} onChange={(e) => setDays(Number(e.target.value))}>
            {[7, 30, 90, 365].map((d) => <option key={d} value={d}>{d}</option>)}
          </select>
          <input className="input mt-1" type="number" min={1} max={3660} value={days} onChange={(e) => setDays(Number(e.target.value))} aria-label="custom days" /></div>
        <div><label className="label">Count</label><input className="input" type="number" min={1} max={500} value={count} onChange={(e) => setCount(Number(e.target.value))} /></div>
        <div><label className="label">Max uses</label><input className="input" type="number" min={1} value={maxUses} onChange={(e) => setMaxUses(Number(e.target.value))} /></div>
        <div className="flex items-end"><button className="btn w-full" onClick={generate}>Generate</button></div>
      </div>
      {error && <p className="text-sm text-red-400">{error}</p>}
      {generated.length > 0 && (
        <div className="card border-amber-700/50">
          <p className="mb-2 text-sm text-amber-400">Shown once - codes are stored only as hashes. Copy them now.</p>
          <pre className="whitespace-pre-wrap font-mono text-sm">{generated.join("\n")}</pre>
        </div>
      )}
      <div className="overflow-x-auto rounded-xl border border-slate-800">
        <table className="w-full text-sm">
          <thead className="bg-slate-900 text-xs uppercase text-slate-400"><tr>
            {["hint", "plan", "duration_days", "status", "uses", "expires_at", ""].map((c) => <th key={c} className="px-3 py-2 text-left">{c}</th>)}
          </tr></thead>
          <tbody>{(codes.data ?? []).map((c) => (
            <tr key={String(c.id)} className="border-t border-slate-800">
              <td className="px-3 py-2 font-mono">{String(c.hint)}</td><td>{String(c.plan)}</td><td>{String(c.duration_days)}</td>
              <td>{String(c.status)}</td><td>{String(c.current_uses)}/{String(c.max_uses)}</td><td>{String(c.expires_at ?? "-")}</td>
              <td>{c.status === "active" && <button className="text-red-400" onClick={async () => { await post(`/api/admin/codes/${c.id}/revoke`); codes.reload(); }}>revoke</button>}</td>
            </tr>))}</tbody>
        </table>
      </div>
    </div>
  );
}

function Trades() {
  const trades = useLoad<Json[]>("/api/admin/trades/recent?limit=100");
  return <Table rows={trades.data ?? []} columns={["created_at", "mode", "market_ticker", "side", "quantity", "price", "status", "reason", "model_version"]} />;
}

function Logs() {
  const [filter, setFilter] = useState("");
  const logs = useLoad<Json[]>(`/api/admin/audit-logs?limit=200${filter ? `&action=${encodeURIComponent(filter)}` : ""}`, [filter]);
  return (
    <div className="space-y-3">
      <input className="input max-w-sm" placeholder="Filter by action prefix (e.g. order., admin.)" value={filter} onChange={(e) => setFilter(e.target.value)} />
      <Table rows={logs.data ?? []} columns={["created_at", "action", "actor_type", "actor_user_id", "target_type", "target_id", "details"]} />
    </div>
  );
}

function KillSwitch() {
  const stats = useLoad<Json>("/api/admin/stats");
  const [reason, setReason] = useState("");
  const [msg, setMsg] = useState<string | null>(null);
  const active = Boolean(stats.data?.global_kill_switch);
  async function toggle(next: boolean) {
    if (!reason.trim()) { setMsg("A reason is required."); return; }
    if (next && !confirm("Activate the GLOBAL kill switch? All automated trading stops and open orders are cancelled.")) return;
    try {
      await post("/api/admin/kill-switch", { active: next, reason });
      setMsg(next ? "Global kill switch ACTIVATED" : "Global kill switch deactivated");
      stats.reload();
    } catch (e) {
      setMsg((e as Error).message);
    }
  }
  return (
    <div className="card max-w-xl space-y-4">
      <p>Status: <b className={active ? "text-red-400" : "text-emerald-400"}>{active ? "ACTIVE - trading paused" : "off"}</b></p>
      <input className="input" placeholder="Reason (required, audited)" value={reason} onChange={(e) => setReason(e.target.value)} />
      <div className="flex gap-3">
        <button className="btn-danger" onClick={() => toggle(true)} disabled={active}>ACTIVATE GLOBAL STOP</button>
        <button className="btn" onClick={() => toggle(false)} disabled={!active}>Deactivate</button>
      </div>
      {msg && <p className="text-sm">{msg}</p>}
    </div>
  );
}

export default function AdminPage() {
  const [me, setMe] = useState<Me | null>(null);
  const [state, setState] = useState<"loading" | "login" | "forbidden" | "ok">("loading");
  const [tab, setTab] = useState<Tab>("Overview");
  const load = useCallback(() => {
    api<Me>("/api/auth/me")
      .then((m) => { setMe(m); setState(m.role === "admin" ? "ok" : "forbidden"); })
      .catch((e) => setState(e instanceof ApiError && e.status === 401 ? "login" : "forbidden"));
  }, []);
  useEffect(load, [load]);

  if (state === "loading") return <p>Loading...</p>;
  if (state === "login") return <div className="card space-y-3"><p>Admin login</p><TelegramLogin onLogin={load} /></div>;
  if (state === "forbidden") return <p className="text-red-400">Admin access required.</p>;
  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <h1 className="text-2xl font-semibold">Admin dashboard</h1>
        <span className="text-sm text-slate-400">@{me?.telegram_username}</span>
      </div>
      <div className="flex flex-wrap gap-2">
        {TABS.map((t) => (
          <button key={t} onClick={() => setTab(t)}
                  className={`rounded-lg px-3 py-1.5 text-sm ${tab === t ? "bg-emerald-700" : "bg-slate-800 hover:bg-slate-700"}`}>{t}</button>
        ))}
      </div>
      {tab === "Overview" && <Overview />}
      {tab === "Performance" && <Performance />}
      {tab === "Users" && <Users />}
      {tab === "Codes" && <Codes />}
      {tab === "Trades" && <Trades />}
      {tab === "Logs" && <Logs />}
      {tab === "Kill switch" && <KillSwitch />}
    </div>
  );
}
