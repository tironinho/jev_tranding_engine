import { Panel } from "@/components/Shell";
import { num, pct } from "@/lib/utils";

export type PolicyComparison = {
  enabled: boolean;
  mode: string;
  initial_equity: number;
  sample_count: number;
  provider_calls: number;
  policies: Array<{
    id: string; label: string; decisions: number; accepted: number; closed: number;
    open_positions: number; win_rate: number | null; net_pnl: number;
    fees: number; interest: number; equity: number; rejects: Record<string, number>;
  }>;
};

export function PolicyPaperComparison({ data }: { data: PolicyComparison | null }) {
  if (!data?.enabled) return null;
  return <Panel title="COMPARAÇÃO DE POLÍTICAS · PAPER">
    <p className="mb-3 text-xs text-mute">
      Resultados simulados, separados do saldo real. Cada política começou com {num(data.initial_equity)} USDT.
      {" "}{data.sample_count} candidatos amostrados · {data.provider_calls} avaliações do JEV concluídas.
      {" "}Taxas, slippage do book e juros estimados entram no resultado.
    </p>
    <div className="overflow-x-auto">
      <table className="w-full text-left font-mono text-xs">
        <thead className="text-mute"><tr>
          {["POLÍTICA", "DECISÕES", "ENTRADAS", "ABERTAS", "FECHAMENTOS", "ACERTO", "LÍQUIDO USDT", "TAXAS USDT", "JUROS USDT", "PATRIMÔNIO USDT"].map(label =>
            <th key={label} className="px-2 py-2 font-normal">{label}</th>)}
        </tr></thead>
        <tbody>{data.policies.map(row => <tr key={row.id} className="border-t border-line">
          <td className="px-2 py-2">{row.label}</td>
          <td className="px-2 py-2">{row.decisions}</td><td className="px-2 py-2">{row.accepted}</td>
          <td className="px-2 py-2">{row.open_positions}</td><td className="px-2 py-2">{row.closed}</td>
          <td className="px-2 py-2">{pct(row.win_rate)}</td>
          <td className="px-2 py-2">{row.closed ? num(row.net_pnl, 4) : "SEM FECHAMENTO"}</td>
          <td className="px-2 py-2">{num(row.fees, 4)}</td><td className="px-2 py-2">{num(row.interest, 4)}</td>
          <td className="px-2 py-2">{num(row.equity, 4)}</td>
        </tr>)}</tbody>
      </table>
    </div>
    <details className="mt-3 text-xs text-mute"><summary>Motivos de rejeição por política</summary>
      <ul className="mt-2 space-y-1">{data.policies.map(row => <li key={row.id}>
        {row.label}: {Object.entries(row.rejects).sort((a,b) => b[1]-a[1]).slice(0,3)
          .map(([reason,count]) => `${reason} (${count})`).join(", ") || "Sem rejeição registrada"}
      </li>)}</ul>
    </details>
  </Panel>;
}
