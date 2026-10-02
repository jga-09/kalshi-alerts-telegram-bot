import type { Metadata } from "next";
import Link from "next/link";
import "./globals.css";

export const metadata: Metadata = {
  title: "Kalshi AI",
  description: "AI-assisted Kalshi market analysis. Probabilities are estimates; trading involves risk.",
  robots: { index: false },
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body className="min-h-screen">
        <header className="border-b border-slate-800">
          <nav className="mx-auto flex max-w-6xl items-center gap-6 px-6 py-4 text-sm">
            <Link href="/" className="font-semibold text-emerald-400">Kalshi AI</Link>
            <Link href="/billing" className="text-slate-300 hover:text-white">Billing</Link>
            <Link href="/admin" className="text-slate-300 hover:text-white">Admin</Link>
            <Link href="/legal/risk" className="ml-auto text-slate-400 hover:text-white">Risk disclosure</Link>
          </nav>
        </header>
        <main className="mx-auto max-w-6xl px-6 py-8">{children}</main>
        <footer className="mx-auto max-w-6xl px-6 pb-10 text-xs text-slate-500">
          AI predictions are estimates, not guarantees. Trading involves risk and automated trading can result in
          losses. Past performance does not guarantee future results. You control your own Kalshi account and the API
          permissions you grant. ·{" "}
          <Link href="/legal/terms">Terms</Link> · <Link href="/legal/privacy">Privacy</Link> ·{" "}
          <Link href="/legal/subscription">Subscription terms</Link>
        </footer>
      </body>
    </html>
  );
}
