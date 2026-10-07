import "server-only";

const base = () => process.env.ENGINE_API_URL ?? "http://127.0.0.1:8000";

export type EngineResult<T> = {
  ok: boolean;
  status: number;
  data: T | null;
  error?: string;
};

export async function engineFetch<T>(path: string, init?: RequestInit): Promise<EngineResult<T>> {
  const headers = new Headers(init?.headers);
  headers.set("accept", "application/json");
  const secret = process.env.ENGINE_API_SECRET;
  if (secret) headers.set("authorization", `Bearer ${secret}`);
  try {
    const response = await fetch(`${base()}${path}`, { ...init, headers, cache: "no-store" });
    const text = await response.text();
    const data = text ? (JSON.parse(text) as T) : null;
    return { ok: response.ok, status: response.status, data };
  } catch {
    return { ok: false, status: 0, data: null, error: "ENGINE_OFFLINE" };
  }
}

export function engineBaseUrl() {
  return base();
}

export function engineAuthHeaders(): HeadersInit {
  const secret = process.env.ENGINE_API_SECRET;
  return secret ? { authorization: `Bearer ${secret}` } : {};
}
