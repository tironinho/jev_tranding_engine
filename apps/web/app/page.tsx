import { BinanceBalancePanel, PaperBookPanel, type BinanceBalance, type PaperBook } from "@/components/Balances";
import { DecisionTable, PositionTable } from "@/components/DecisionTable";
import { EngineStatus, Panel } from "@/components/Shell";
import { StrategyCard, type StrategyCardData } from "@/components/StrategyCard";
import { SymbolTicker } from "@/components/SymbolTicker";
import { engineFetch } from "@/lib/engine";

export const dynamic = "force-dynamic";

type Overview = {
  status: {
    engine: string;
    trading_enabled: boolean;
    live_armed: boolean;
    market_type: string;
    binance: { status: string };
    openai: { status: string };
    jev: { status: string; is_mock: boolean };
    database: { mode: string; healthy: boolean };
  };
  strategies: StrategyCardData[];
  tickers: Array<{ symbol: string; price: number | null; spread_bps?: number | null; funding_rate?: number | null; open_interest?: number | null; status?: string; stale?: boolean }>;
  positions: Array<{ position_id?: string; strategy: string; symbol: string; side: string; quantity: number; entry: number; stop: number; target: number; notional?: number; unrealized: number; target_pnl?: number; stop_pnl?: number }>;
  binance_balance?: BinanceBalance;
  paper?: PaperBook;
};

export default async function OverviewPage() {
  const [overview, decisions] = await Promise.all([
    engineFetch<Overview>("/api/overview"),
    engineFetch<{ rows: Parameters<typeof DecisionTable>[0]["rows"] }>("/api/decisions?limit=12"),
  ]);
  if (!overview.ok || !overview.data) {
    return (
      <div className="grid gap-4">
        <EngineStatus status={null} />
        <Panel title="OVERVIEW">
          <div className="font-mono text-xs tracking-[0.16em] text-mute">ENGINE OFFLINE — NO DATA</div>
        </Panel>
      </div>
    );
  }
  const data = overview.data;
  return (
    <div className="grid gap-4">
      <EngineStatus status={data.status} />
      <div className="grid gap-3 lg:grid-cols-2">
        <BinanceBalancePanel balance={data.binance_balance} />
        <PaperBookPanel book={data.paper} />
      </div>
      <div className="grid gap-3 md:grid-cols-3">
        {data.tickers.map((ticker) => (
          <SymbolTicker key={ticker.symbol} ticker={ticker} />
        ))}
      </div>
      <div className="grid gap-3 lg:grid-cols-3">
        {data.strategies.map((card) => (
          <StrategyCard key={card.key} card={card} liveArmed={data.status.live_armed} />
        ))}
      </div>
      <Panel title="POSITIONS">
        <PositionTable rows={data.positions} />
      </Panel>
      <Panel title="LATEST DECISIONS">
        <DecisionTable rows={decisions.ok ? (decisions.data?.rows ?? []) : null} />
      </Panel>
    </div>
  );
}
