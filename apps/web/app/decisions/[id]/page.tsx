import { DecisionInspector } from "@/components/Inspector";
import { engineFetch } from "@/lib/engine";

export const dynamic = "force-dynamic";

export default async function DecisionPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  const result = await engineFetch<Record<string, unknown>>(`/api/decisions/${id}`);
  return <DecisionInspector payload={result.ok ? result.data : null} />;
}
