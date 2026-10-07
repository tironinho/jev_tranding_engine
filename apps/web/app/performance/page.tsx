import { DrawdownChart, EquityCurve, PnLChart } from "@/components/Charts";
import { EngineLogs, RiskPanel } from "@/components/Inspector";
import { PerformanceComparison } from "@/components/PerformanceComparison";
import { Panel } from "@/components/Shell";
import { engineFetch } from "@/lib/engine";

export const dynamic = "force-dynamic";

export default async function PerformancePage() {
  const [equity, compare, trades, limits, logs] = await Promise.all([
    engineFetch<{ series: Record<string, Array<{ t: string; equity: number; indexed: number | null; mark?: boolean }>>; drawdown: Record<string, Array<{ t: string; equity: number; drawdown: number }>> }>("/api/performance/equity"),
    engineFetch<Record<string, number | null>>("/api/performance/compare"),
    engineFetch<{ rows: Array<{ closed_at: string; strategy: string; net_pnl: number }> }>("/api/trades"),
    engineFetch<Record<string, number | string>>("/api/risk/limits"),
    engineFetch<{ rows: Array<{ timestamp?: string; kind?: string; message?: string }> }>("/api/events"),
  ]);
  return (
    <div className="grid gap-4">
      <Panel title="EQUITY — TRÊS CONTAS, MESMO CAPITAL INICIAL">
        <EquityCurve series={equity.ok ? equity.data?.series ?? null : null} />
      </Panel>
      <div className="grid gap-4 lg:grid-cols-2">
        <Panel title="DRAWDOWN">
          <DrawdownChart series={equity.ok ? equity.data?.drawdown ?? null : null} />
        </Panel>
        <Panel title="NET PNL POR TRADE">
          <PnLChart trades={trades.ok ? trades.data?.rows ?? [] : null} />
        </Panel>
      </div>
      <Panel title="COMPARAÇÃO">
        <PerformanceComparison report={compare.ok ? compare.data : null} />
      </Panel>
      <div className="grid gap-4 lg:grid-cols-2">
        <Panel title="RISK">
          <RiskPanel limits={limits.ok ? limits.data : null} />
        </Panel>
        <Panel title="ENGINE LOG">
          <EngineLogs rows={logs.ok ? logs.data?.rows ?? [] : null} />
        </Panel>
      </div>
    </div>
  );
}
