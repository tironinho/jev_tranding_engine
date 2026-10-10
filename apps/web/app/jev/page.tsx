import { JevDesk, type JevReview } from "@/components/JevDesk";
import { engineFetch } from "@/lib/engine";

export const dynamic = "force-dynamic";

export default async function JevPage({ searchParams }: { searchParams: Promise<{ id?: string }> }) {
  const { id } = await searchParams;
  const result = await engineFetch<{ rows: JevReview[]; history?: "postgres" | "memory" | "unavailable" }>("/api/jev?limit=40");
  const rows = result.ok ? result.data?.rows ?? [] : null;
  const selected = rows?.find((row) => row.decision_id === id) ?? rows?.[0] ?? null;
  const detail = selected
    ? await engineFetch<{
        risk?: { accepted?: boolean; reject_reasons?: string[] } | null;
        orders?: Array<{ status?: string }>;
        fills?: Array<Record<string, unknown>>;
        trade?: Record<string, unknown> | null;
      }>(`/api/decisions/${selected.decision_id}`)
    : null;
  return <JevDesk rows={rows} selected={selected} history={result.data?.history} execution={detail?.ok ? detail.data ?? null : null} />;
}
