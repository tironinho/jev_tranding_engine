import { Panel } from "@/components/Shell";
import { type DecisionRow } from "@/components/DecisionTable";
import { shortTime } from "@/lib/utils";

export function DecisionDiagnostics({ rows }: { rows: DecisionRow[] | null }) {
  if (!rows) return <Panel title="POR QUE NÃO ABRIU?"><p>Consulta indisponível.</p></Panel>;
  const counts: Record<string, number> = {};
  const blockers: Record<string, number> = {};
  for (const row of rows) {
    const decision = row.strategies.baseline_jev;
    if (!decision) continue;
    const risk = row.risk?.baseline_jev;
    const orders = row.execution?.baseline_jev ?? [];
    const stage = orders.length ? "Com ordem registrada" : decision.signal_status === "expired" ? "Sinal expirado"
      : decision.action === "NO_TRADE" ? (decision.metadata?.baseline_action === "NO_TRADE" ? "Baseline sem sinal" : "Plano / Jev vetou")
      : risk ? (risk.accepted ? "Risco aprovado · sem ordem registrada" : "Risco rejeitou") : "Aguardando risco / sem registro";
    counts[stage] = (counts[stage] ?? 0) + 1;
    const reasons = risk && !risk.accepted ? risk.reject_reasons : decision.action === "NO_TRADE" ? decision.reason_codes ?? [] : [];
    for (const reason of reasons.filter(r => r !== "RISK_REJECTED" && r !== "BASELINE_NO_TRADE" && !r.startsWith("CLASS_"))) {
      blockers[reason] = (blockers[reason] ?? 0) + 1;
    }
  }
  return <Panel title="POR QUE NÃO ABRIU? · BASELINE + JEV">
    <p className="mb-3 text-xs text-mute">Últimas {rows.length} oportunidades disponíveis nesta sessão do motor, {shortTime(rows.at(-1)?.timestamp)}–{shortTime(rows[0]?.timestamp)} (Brasília). Não é todo o histórico. Aprovação de risco não confirma execução.</p>
    <div className="grid gap-3 sm:grid-cols-3">{Object.entries(counts).map(([stage, count]) => <div key={stage} className="border border-line p-3"><div className="text-xs text-mute">{stage}</div><div className="font-mono text-xl">{count}</div></div>)}</div>
    <p className="mt-3 text-xs">{Object.entries(blockers).sort((a,b) => b[1]-a[1]).map(([r,n]) => `${r}: ${n}`).join(" · ") || "Sem veto registrado no recorte."}</p>
    <p className="mt-2 text-xs text-mute">Score do baseline não é probabilidade. p(alvo) é uma estimativa não calibrada do Jev. EV usa custos e cenário de estresse; não é lucro previsto ou garantido. Abra a decisão para ver todos os planos e registros.</p>
  </Panel>;
}
