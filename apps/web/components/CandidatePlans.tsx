import { num, pct } from "@/lib/utils";

export type CandidatePlan = { plan_id: string; entry: number; stop: number; target: number;
  gross_rr: number; net_rr: number; raw_probability?: number; stress_probability?: number;
  break_even_probability: number; expected_net_r?: number; eligible: boolean; reason?: string;
  interest_known?: boolean; horizon_minutes?: number };

export function CandidatePlans({ plans, selected }: { plans?: CandidatePlan[]; selected?: string }) {
  if (!plans?.length) return <p className="text-xs text-mute">Esta decisão não possui alternativas de R:R registradas.</p>;
  return <div className="overflow-x-auto text-xs"><table className="w-full text-left"><thead><tr>{["Plano", "Entrada / stop / alvo", "R:R bruto / líquido", "p Jev / estresse / equilíbrio", "EV-R estresse", "Resultado"].map(h => <th key={h} className="p-2 text-mute font-normal">{h}</th>)}</tr></thead>
    <tbody>{plans.map(p => <tr className="border-t border-line font-mono" key={p.plan_id}><td className="p-2">{p.plan_id}{p.plan_id === selected ? " · ESCOLHIDO" : ""}</td>
      <td className="p-2">{num(p.entry,4)} / {num(p.stop,4)} / {num(p.target,4)}</td><td className="p-2">{num(p.gross_rr)} / {num(p.net_rr)}</td>
      <td className="p-2">{pct(p.raw_probability)} / {pct(p.stress_probability)} / {pct(p.break_even_probability)}</td><td className="p-2">{num(p.expected_net_r,3)}</td>
      <td className="p-2">{p.reason ?? "—"}{p.interest_known === false ? " · juros estimados por estresse" : ""}</td></tr>)}</tbody></table>
    <p className="mt-2 text-mute">Alvos avaliados separadamente, antes da execução. Horizonte {plans[0].horizon_minutes ?? "—"} min. Probabilidades não calibradas; o desconto de estresse não é um intervalo de confiança. Sem probabilidade = plano não avaliado ou falha no provedor.</p></div>;
}
