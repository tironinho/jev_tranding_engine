import Link from "next/link";
import { Empty } from "@/components/Shell";
import { money, num, pct, shortTime, signedClass } from "@/lib/utils";

type Decision = {
  decision_id: string;
  action: string;
  confidence: number;
  signal_status: string;
  reason_codes?: string[];
  metadata?: {
    baseline_action?: string;
    jev_continuation?: number;
    candidate_plans?: Array<{ eligible: boolean; net_rr?: number; reason?: string; expected_net_r?: number }>;
    jev_trade_plan?: { net_rr?: number; expected_net_r?: number };
    openai_regime?: string;
    openai_confidence?: number;
  };
};

export type DecisionRow = {
  opportunity_id: string;
  timestamp: string;
  symbol: string;
  strategies: Record<string, Decision>;
  consensus?: { label?: string } | null;
  risk?: Record<string, { accepted: boolean; reject_reasons: string[] }>;
  execution?: Record<string, Array<{ status?: string }>>;
  execution_note?: Record<string, string | null>;
};

function cell(decision?: Decision) {
  if (!decision) return <span className="text-mute">—</span>;
  if (decision.signal_status === "skipped" || decision.signal_status === "expired") {
    const label = decision.signal_status === "expired" ? "EXPIRED" : "STANDBY";
    return (
      <Link href={`/decisions/${decision.decision_id}`} className="font-mono hover:underline">
        {label}
      </Link>
    );
  }
  const score = `${decision.action} · score ${Math.round(decision.confidence * 100)}`;
  const reason = decision.reason_codes?.filter((code) => code !== "BASELINE_NO_TRADE" && code !== "JEV_CONFIRM" && !code.startsWith("CLASS_")).join(" · ");
  const meta = decision.metadata;
  const notes: string[] = [];
  if (meta?.baseline_action === "NO_TRADE" && typeof meta.openai_regime === "string") {
    const confidence = typeof meta.openai_confidence === "number" ? ` ${Math.round(meta.openai_confidence * 100)}%` : "";
    notes.push(`${meta.openai_regime}${confidence}`);
  }
  if (typeof meta?.jev_continuation === "number") {
    notes.push(`p(alvo) ${Math.round(meta.jev_continuation * 100)}%`);
  }
  const label = [score, reason, notes.join(" · ")].filter(Boolean).join(" · ");
  return (
    <Link href={`/decisions/${decision.decision_id}`} className="font-mono hover:underline">
      {label}
    </Link>
  );
}

function riskCell(row: DecisionRow) {
  const rejected = row.risk?.baseline_jev;
  if (!rejected) return "Não avaliado";
  if (rejected.accepted) return "ACCEPTED";
  const reason = rejected.reject_reasons.find((item) => item !== "RISK_REJECTED") ?? rejected.reject_reasons[0];
  return `REJECTED ${reason ?? ""}`.trim();
}

export function DecisionTable({ rows }: { rows: DecisionRow[] | null }) {
  if (!rows) return <Empty label="NO DATA" />;
  if (!rows.length) return <Empty label="NO DATA" />;
  return (
    <div className="overflow-x-auto">
      <table className="w-full text-left text-[11px]">
        <thead className="text-mute">
          <tr className="border-b border-line">
            {["BRASÍLIA", "SYMBOL", "BASELINE · PAPER", "JEV · ESTIMATIVA", "CONSENSUS", "RISCO JEV", "EXECUÇÃO JEV", "R:R / EV-R EST."].map((head) => (
              <th key={head} className="px-2 py-2 font-normal tracking-[0.12em]">
                {head}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr key={row.opportunity_id} className="border-b border-line/70">
              <td className="px-2 py-2 font-mono">{shortTime(row.timestamp)}</td>
              <td className="px-2 py-2">{row.symbol}</td>
              <td className="px-2 py-2">{cell(row.strategies.baseline)}</td>
              <td className="px-2 py-2">{cell(row.strategies.baseline_jev)}</td>
              <td className="px-2 py-2 font-mono">{row.consensus?.label ?? "NO DATA"}</td>
              <td className="px-2 py-2 font-mono">{riskCell(row)}</td>
              <td className="px-2 py-2 font-mono">{row.execution?.baseline_jev?.map(o => o.status).join(" / ") || row.execution_note?.baseline_jev || "Sem ordem registrada"}</td>
              <td className="px-2 py-2 font-mono">{num(row.strategies.baseline_jev?.metadata?.jev_trade_plan?.net_rr)} / {num(row.strategies.baseline_jev?.metadata?.jev_trade_plan?.expected_net_r)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

export function PositionTable({
  rows,
  exchange,
}: {
  rows: Array<{
    position_id?: string;
    strategy: string;
    symbol: string;
    side: string;
    quantity: number;
    entry: number;
    stop: number;
    target: number;
    notional?: number;
    leverage?: number | null;
    mode?: string;
    unrealized: number;
    target_pnl?: number;
    stop_pnl?: number;
    protection_status?: string;
  }> | null;
  exchange?: {
    rows: Array<{
      asset: string;
      net_quantity: number;
      free?: number;
      locked?: number;
      difference: number;
      mark?: number | null;
      value_usdt?: number | null;
      status: string;
    }>;
  };
}) {
  if (!rows) return <Empty label="Falha na consulta de posições do robô" />;
  const visible = rows.filter((row) => row.protection_status !== "RESIDUAL");
  if (!visible.length) return <Empty label="Sem posições gerenciadas · resíduos abaixo do mínimo estão na conta abaixo" />;
  const exposure = new Map((exchange?.rows ?? []).map((row) => [row.asset, row]));
  const reconciled = Boolean(exchange);
  return (
    <div className="overflow-x-auto">
      <table className="w-full text-left text-[11px]">
        <thead className="text-mute">
          <tr className="border-b border-line">
            {["STRATEGY", "SYMBOL", "SIDE", "QTD ROBÔ", ...(reconciled ? ["LÍQ. BINANCE", "LIVRE", "TRAVADO", "DIF. CONTA", "VALOR CONTA $", "RESÍDUO $", "STATUS"] : []), "VALOR ROBÔ $", "LEV", "FILL ROBÔ", "PNL ROBÔ", "TARGET $", "STOP $"].map((head) => (
              <th key={head} className="px-2 py-2 font-normal tracking-[0.12em]">
                {head}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {visible.map((row) => {
            const account = exposure.get(row.symbol.endsWith("USDT") ? row.symbol.slice(0, -4) : row.symbol);
            const status = row.protection_status === "PROTECTED" && account?.status === "RESIDUAL"
              ? "PROTEGIDO + RESÍDUO"
              : row.protection_status ?? account?.status ?? "NO DATA";
            return (
              <tr key={row.position_id ?? `${row.strategy}-${row.symbol}-${row.entry}`} className="border-b border-line/70 font-mono">
                <td className="px-2 py-2">{row.strategy}</td>
                <td className="px-2 py-2">{row.symbol}</td>
                <td className="px-2 py-2">{row.side}</td>
                <td className="px-2 py-2">{row.quantity.toFixed(8)}</td>
                {reconciled ? (
                  <>
                    <td className="px-2 py-2">{account ? num(account.net_quantity, 8) : "NO DATA"}</td>
                    <td className="px-2 py-2">{account?.free == null ? "—" : num(account.free, 8)}</td>
                    <td className="px-2 py-2">{account?.locked == null ? "—" : num(account.locked, 8)}</td>
                    <td className={`px-2 py-2 ${signedClass(account?.difference)}`}>{account ? num(account.difference, 8) : "NO DATA"}</td>
                    <td className="px-2 py-2">{account?.value_usdt == null ? "NO DATA" : num(account.value_usdt, 4)}</td>
                    <td className={`px-2 py-2 ${signedClass(account && account.mark != null ? account.difference * account.mark : null)}`}>
                      {account?.mark == null ? "NO DATA" : positionMoney(account.difference * account.mark)}
                    </td>
                    <td className="px-2 py-2">{status}</td>
                  </>
                ) : null}
                <td className="px-2 py-2">{row.notional == null ? "NO DATA" : num(row.notional, 2)}</td>
                <td className="px-2 py-2">{leverageLabel(row.leverage)}</td>
                <td className="px-2 py-2">{row.entry.toFixed(4)}</td>
                <td className={`px-2 py-2 ${signedClass(row.unrealized)}`}>{positionMoney(row.unrealized)}</td>
                <td className={`px-2 py-2 ${signedClass(row.target_pnl)}`}>{positionMoney(row.target_pnl)}</td>
                <td className={`px-2 py-2 ${signedClass(row.stop_pnl)}`}>{positionMoney(row.stop_pnl)}</td>
              </tr>
            );
          })}
        </tbody>
      </table>
      <div className="px-2 pt-2 text-[10px] text-mute">QTD ROBÔ, VALOR ROBÔ $ e FILL ROBÔ pertencem à ordem executada pelo sistema. LÍQ. BINANCE e VALOR CONTA $ incluem todo o saldo da moeda; RESÍDUO $ é a diferença marcada entre a conta e a quantidade do robô. LIVRE e TRAVADO vêm da conta no mesmo instante. A Binance pode mostrar um preço médio e um PnL históricos da conta diferentes do fill e do PNL ROBÔ desta ordem. LEV é o nocional marcado dividido pela margem travada. PNL ROBÔ, TARGET $ e STOP $ usam somente o trade do robô e incluem as taxas configuradas.</div>
    </div>
  );
}

function leverageLabel(value: number | null | undefined) {
  if (value == null || !Number.isFinite(value) || value <= 0) return "NO DATA";
  const rounded = Math.round(value * 10) / 10;
  return `${Number.isInteger(rounded) ? rounded.toFixed(0) : rounded.toFixed(1)}x`;
}

function positionMoney(value: number | null | undefined) {
  if (value == null || Number.isNaN(value)) return "NO DATA";
  const absolute = Math.abs(value);
  if (absolute === 0 || absolute >= 0.01) return money(value);
  const digits = absolute >= 0.0001 ? 4 : 8;
  const formatted = absolute.toLocaleString("en-US", { minimumFractionDigits: digits, maximumFractionDigits: digits });
  return `${value > 0 ? "+" : "-"}${formatted}`;
}

export function formatConfidence(value: number) {
  return pct(value);
}
