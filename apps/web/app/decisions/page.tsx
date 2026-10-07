import { DecisionTable } from "@/components/DecisionTable";
import { Panel } from "@/components/Shell";
import { engineFetch } from "@/lib/engine";

export const dynamic = "force-dynamic";

export default async function DecisionsPage() {
  const result = await engineFetch<{ rows: Parameters<typeof DecisionTable>[0]["rows"] }>("/api/decisions?limit=200");
  return (
    <Panel title="DECISIONS">
      <DecisionTable rows={result.ok ? result.data?.rows ?? [] : null} />
    </Panel>
  );
}
