import { JevDesk, type JevReview } from "@/components/JevDesk";
import { engineFetch } from "@/lib/engine";

export const dynamic = "force-dynamic";

export default async function JevPage({ searchParams }: { searchParams: Promise<{ id?: string }> }) {
  const { id } = await searchParams;
  const result = await engineFetch<{ rows: JevReview[] }>("/api/jev?limit=40");
  const rows = result.ok ? result.data?.rows ?? [] : null;
  const selected = rows?.find((row) => row.decision_id === id) ?? rows?.[0] ?? null;
  return <JevDesk rows={rows} selected={selected} />;
}
