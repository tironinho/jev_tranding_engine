import { DecisionTable } from "@/components/DecisionTable";
import { DecisionDiagnostics } from "@/components/DecisionDiagnostics";
import { Panel } from "@/components/Shell";
import { engineFetch } from "@/lib/engine";

export const dynamic = "force-dynamic";

export default async function DecisionsPage() {
  const result = await engineFetch<{ rows: Parameters<typeof DecisionTable>[0]["rows"] }>("/api/decisions?limit=200");
  return (
    <div className="grid gap-4"><DecisionDiagnostics rows={result.ok ? result.data?.rows ?? [] : null} /><Panel title="DECISIONS · HORÁRIO DE BRASÍLIA">
      <DecisionTable rows={result.ok ? result.data?.rows ?? [] : null} />
    </Panel></div>
  );
}
