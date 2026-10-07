import { engineAuthHeaders, engineBaseUrl } from "@/lib/engine";

export const dynamic = "force-dynamic";

async function proxy(request: Request, path: string[], method: string) {
  const incoming = new URL(request.url);
  const target = `${engineBaseUrl()}/api/${path.join("/")}${incoming.search}`;
  const headers = new Headers(engineAuthHeaders());
  const contentType = request.headers.get("content-type");
  if (contentType) headers.set("content-type", contentType);
  const body = method === "GET" || method === "HEAD" ? undefined : await request.text();
  try {
    const response = await fetch(target, { method, headers, body, cache: "no-store" });
    const text = await response.text();
    return new Response(text, {
      status: response.status,
      headers: { "content-type": response.headers.get("content-type") ?? "application/json" },
    });
  } catch {
    return Response.json({ error: "ENGINE_OFFLINE" }, { status: 503 });
  }
}

export async function GET(request: Request, context: { params: Promise<{ path: string[] }> }) {
  const { path } = await context.params;
  return proxy(request, path, "GET");
}

export async function POST(request: Request, context: { params: Promise<{ path: string[] }> }) {
  const { path } = await context.params;
  return proxy(request, path, "POST");
}

export async function PATCH(request: Request, context: { params: Promise<{ path: string[] }> }) {
  const { path } = await context.params;
  return proxy(request, path, "PATCH");
}
