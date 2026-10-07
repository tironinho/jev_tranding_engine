import Link from "next/link";
import { Empty } from "@/components/Shell";
import { pct, shortTime } from "@/lib/utils";

type Decision = {
  decision_id: string;
  action: string;
  confidence: number;
  signal_status: string;
  reason_codes?: string[];
};

type Row = {
  opportunity_id: string;
  timestamp: string;
  symbol: string;
  strategies: Record<string, Decision>;
  consensus?: { label?: string } | null;
  risk?: Record<string, { accepted: boolean; reject_reasons: string[] }>;
};

function cell(decision?: Decision) {
  if (!decision) return <span className="text-mute">—</span>;
  const label = decision.signal_status === "skipped" ? "STANDBY" : `${decision.action} ${Math.round(decision.confidence * 100)}%`;
  return (
    <Link href={`/decisions/${decision.decision_id}`} className="font-mono hover:underline">
      {label}
    </Link>
  );
}

function riskCell(row: Row) {
  const risks = Object.values(row.risk ?? {});
  if (!risks.length) return "—";
  const rejected = risks.find((item) => !item.accepted);
  if (!rejected) return "ACCEPTED";
  const reason = rejected.reject_reasons.find((item) => item !== "RISK_REJECTED") ?? rejected.reject_reasons[0];
  return `REJECTED ${reason ?? ""}`.trim();
}

export function DecisionTable({ rows }: { rows: Row[] | null }) {
  if (!rows) return <Empty label="NO DATA" />;
  if (!rows.length) return <Empty label="NO DATA" />;
  return (
    <div className="overflow-x-auto">
      <table className="w-full text-left text-[11px]">
        <thead className="text-mute">
          <tr className="border-b border-line">
            {["TIME", "SYMBOL", "BASELINE", "JEV", "OPENAI+JEV", "CONSENSUS", "RISK"].map((head) => (
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
              <td className="px-2 py-2">{cell(row.strategies.baseline_openai_jev)}</td>
              <td className="px-2 py-2 font-mono">{row.consensus?.label ?? "NO DATA"}</td>
              <td className="px-2 py-2 font-mono">{riskCell(row)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

export function PositionTable({
  rows,
}: {
  rows: Array<{
    strategy: string;
    symbol: string;
    side: string;
    quantity: number;
    entry: number;
    stop: number;
    target: number;
    unrealized: number;
  }> | null;
}) {
  if (!rows?.length) return <Empty label="NO DATA" />;
  return (
    <div className="overflow-x-auto">
      <table className="w-full text-left text-[11px]">
        <thead className="text-mute">
          <tr className="border-b border-line">
            {["STRATEGY", "SYMBOL", "SIDE", "QTY", "ENTRY", "STOP", "TARGET", "UNREAL"].map((head) => (
              <th key={head} className="px-2 py-2 font-normal tracking-[0.12em]">
                {head}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr key={`${row.strategy}-${row.symbol}`} className="border-b border-line/70 font-mono">
              <td className="px-2 py-2">{row.strategy}</td>
              <td className="px-2 py-2">{row.symbol}</td>
              <td className="px-2 py-2">{row.side}</td>
              <td className="px-2 py-2">{row.quantity.toFixed(4)}</td>
              <td className="px-2 py-2">{row.entry.toFixed(4)}</td>
              <td className="px-2 py-2">{row.stop.toFixed(4)}</td>
              <td className="px-2 py-2">{row.target.toFixed(4)}</td>
              <td className="px-2 py-2">{row.unrealized.toFixed(2)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

export function formatConfidence(value: number) {
  return pct(value);
}
