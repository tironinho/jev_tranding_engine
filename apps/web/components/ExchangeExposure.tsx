"use client";

import { useEffect } from "react";
import { useRouter } from "next/navigation";
import { num } from "@/lib/utils";

export type ExchangeState = {
  status: string;
  observed_at?: number;
  blocks_entry: boolean;
  rows: Array<{ asset: string; net_quantity: number; free?: number; locked?: number;
    borrowed?: number; interest?: number; value_usdt: number | null;
    tracked_quantity: number; difference: number; status: string }>;
};

const labels: Record<string, string> = { CASH: "Caixa", MATCHED: "Conciliado",
  RESIDUAL: "Resíduo / abaixo do mínimo", MISMATCH: "Divergência", UNPRICED: "Sem cotação" };

export function ExchangeExposure({ data }: { data?: ExchangeState }) {
  const router = useRouter();
  useEffect(() => {
    const timer = setInterval(() => { if (!document.hidden) router.refresh(); }, 10000);
    return () => clearInterval(timer);
  }, [router]);
  if (!data || data.status === "UNAVAILABLE") return <p role="alert">Não foi possível consultar a Binance. A ausência de dados não significa conta sem posições.</p>;
  return <div className="overflow-x-auto text-xs">
    <p className="mb-3" role="status">{data.blocks_entry ? "Entradas bloqueadas: exposição divergente ou sem cotação." : "Exposição conciliada; resíduos continuam visíveis."}
      {data.observed_at ? ` Consulta: ${new Date(data.observed_at * 1000).toLocaleString("pt-BR", { timeZone: "America/Sao_Paulo" })} (Brasília).` : ""}</p>
    <table className="w-full text-left"><thead><tr>
      {["Ativo", "Quantidade líquida", "Livre", "Travado", "Emprestado", "Juros (ativo)", "Valor USDT", "Robô (qtd)", "Diferença", "Situação"].map(h => <th className="p-2 font-normal text-mute" key={h}>{h}</th>)}
    </tr></thead><tbody>{data.rows.map(row => <tr key={row.asset} className="border-t border-line font-mono">
      <td className="p-2">{row.asset}</td>
      {[row.net_quantity, row.free, row.locked, row.borrowed, row.interest].map((v, i) => <td className="p-2" key={i}>{v == null ? "—" : num(v, 8)}</td>)}
      <td className="p-2">{row.value_usdt == null ? "Sem cotação" : num(row.value_usdt, 4)}</td>
      <td className="p-2">{row.status === "CASH" ? "—" : num(row.tracked_quantity, 8)}</td>
      <td className="p-2">{row.status === "CASH" ? "—" : num(row.difference, 8)}</td>
      <td className="p-2">{labels[row.status] ?? row.status}</td>
    </tr>)}</tbody></table>
    <p className="mt-3 text-mute">Todos os saldos e débitos da conta, inclusive resíduos. A Binance pode contar cada ativo ou dívida como uma posição; a tabela do robô conta somente trades abertos pelo sistema. Juros são separados do principal na conciliação. Saldo sem ordem de origem não recebe preço de entrada ou PnL inventados. Nenhum resíduo é liquidado automaticamente.</p>
  </div>;
}
