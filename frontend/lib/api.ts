// Same-origin API client. Auth is an httpOnly cookie; state-changing requests echo the CSRF cookie.

export class ApiError extends Error {
  constructor(public status: number, message: string) {
    super(message);
  }
}

function csrfToken(): string {
  if (typeof document === "undefined") return "";
  const match = document.cookie.match(/(?:^|;\s*)kai_csrf=([^;]+)/);
  return match ? decodeURIComponent(match[1]) : "";
}

export async function api<T>(path: string, init: RequestInit = {}): Promise<T> {
  const method = (init.method ?? "GET").toUpperCase();
  const headers = new Headers(init.headers);
  if (init.body && !headers.has("Content-Type")) headers.set("Content-Type", "application/json");
  if (method !== "GET" && method !== "HEAD") headers.set("X-CSRF-Token", csrfToken());
  const res = await fetch(path, { ...init, method, headers, credentials: "same-origin", cache: "no-store" });
  if (!res.ok) {
    let detail = res.statusText;
    try {
      const body = await res.json();
      detail = typeof body.detail === "string" ? body.detail : JSON.stringify(body.detail);
    } catch {
      /* non-JSON error */
    }
    throw new ApiError(res.status, detail);
  }
  return (await res.json()) as T;
}

export const post = <T>(path: string, body?: unknown) =>
  api<T>(path, { method: "POST", body: body === undefined ? undefined : JSON.stringify(body) });

export const put = <T>(path: string, body: unknown) => api<T>(path, { method: "PUT", body: JSON.stringify(body) });

export type Me = {
  id: string;
  telegram_username: string | null;
  first_name: string | null;
  role: "admin" | "customer";
  subscription: { active: boolean; plan: string | null; expires_at: string | null };
};
