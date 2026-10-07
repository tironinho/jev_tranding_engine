import { Empty } from "@/components/Shell";
import { money, num } from "@/lib/utils";

export function PerformanceComparison({
  report,
}: {
  report: {
    baseline_trades?: number;
    jev_trades?: number;
    openai_trades?: number;
    jev_eliminated_signals?: number;
    eliminated_trade_net_pnl?: number;
    incremental_pnl_jev_vs_baseline?: number;
    incremental_pnl_openai_vs_jev?: number;
    ai_cost?: number | null;
    ai_roi?: number | null;
    only_baseline?: number;
    only_jev?: number;
    only_openai?: number;
    all_agreed?: number;
    disagreed?: number;
  } | null;
}) {
  if (!report) return <Empty />;
  const rows = [
    ["Trades baseline", String(report.baseline_trades ?? 0)],
    ["Trades Jev", String(report.jev_trades ?? 0)],
    ["Trades OpenAI+Jev", String(report.openai_trades ?? 0)],
    ["Sinais que o Jev eliminou", String(report.jev_eliminated_signals ?? 0)],
    ["PnL líquido desses trades na baseline", money(report.eliminated_trade_net_pnl)],
    ["PnL incremental Jev vs baseline", money(report.incremental_pnl_jev_vs_baseline)],
    ["PnL incremental OpenAI vs Jev", money(report.incremental_pnl_openai_vs_jev)],
    ["Custo de IA", report.ai_cost == null ? "NO DATA" : money(report.ai_cost)],
    ["AI ROI", report.ai_roi == null ? "NO DATA" : num(report.ai_roi, 2)],
    ["Só baseline", String(report.only_baseline ?? 0)],
    ["Só Jev", String(report.only_jev ?? 0)],
    ["Só OpenAI+Jev", String(report.only_openai ?? 0)],
    ["Acordo", String(report.all_agreed ?? 0)],
    ["Divergência", String(report.disagreed ?? 0)],
  ];
  return (
    <dl className="grid grid-cols-1 gap-2 text-[12px] sm:grid-cols-2">
      {rows.map(([label, value]) => (
        <div key={label} className="flex items-center justify-between gap-3 border-b border-line/60 py-1">
          <dt className="text-mute">{label}</dt>
          <dd className="font-mono">{value}</dd>
        </div>
      ))}
    </dl>
  );
}
