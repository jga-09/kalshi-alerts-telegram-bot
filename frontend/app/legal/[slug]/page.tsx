import { notFound } from "next/navigation";

const API = process.env.API_INTERNAL_URL ?? "http://localhost:8000";
const SLUGS = ["terms", "privacy", "risk", "subscription"];

type Disclosure = { slug: string; version: string; title: string; body_markdown: string };

// Minimal, safe markdown rendering (headings, bullets, paragraphs). Text is rendered as React text - no HTML injection.
function Markdown({ text }: { text: string }) {
  const blocks = text.split(/\n{2,}/);
  return (
    <div className="space-y-3 text-slate-300">
      {blocks.map((block, i) => {
        const lines = block.split("\n");
        if (block.startsWith("# ")) return <h1 key={i} className="text-2xl font-semibold text-white">{block.slice(2)}</h1>;
        if (lines.every((l) => /^\s*(- |\d+\. )/.test(l) || /^\s{2,}/.test(l))) {
          return (
            <ul key={i} className="list-disc space-y-1 pl-6">
              {lines.filter((l) => /^\s*(- |\d+\. )/.test(l)).map((l, j) => (
                <li key={j}>{l.replace(/^\s*(- |\d+\. )/, "").replaceAll("**", "")}</li>
              ))}
            </ul>
          );
        }
        return <p key={i}>{block.replaceAll("**", "")}</p>;
      })}
    </div>
  );
}

export default async function LegalPage({ params }: { params: Promise<{ slug: string }> }) {
  const { slug } = await params;
  if (!SLUGS.includes(slug)) notFound();
  const res = await fetch(`${API}/api/disclosures/${slug}`, { next: { revalidate: 300 } }).catch(() => null);
  if (!res || !res.ok) return <p>Unable to load this document right now.</p>;
  const doc = (await res.json()) as Disclosure;
  return (
    <article className="card max-w-3xl">
      <Markdown text={doc.body_markdown} />
      <p className="mt-6 text-xs text-slate-500">Version {doc.version}</p>
    </article>
  );
}
