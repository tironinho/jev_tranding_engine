import { engineAuthHeaders, engineBaseUrl } from "@/lib/engine";

export const dynamic = "force-dynamic";

export async function GET() {
  try {
    const response = await fetch(`${engineBaseUrl()}/stream`, {
      headers: { accept: "text/event-stream", ...engineAuthHeaders() },
      cache: "no-store",
    });
    return new Response(response.body, {
      status: response.status,
      headers: {
        "content-type": "text/event-stream",
        "cache-control": "no-cache, no-transform",
        connection: "keep-alive",
      },
    });
  } catch {
    return new Response("event: engine_status\ndata: {\"engine\":\"offline\"}\n\n", {
      headers: { "content-type": "text/event-stream" },
    });
  }
}
