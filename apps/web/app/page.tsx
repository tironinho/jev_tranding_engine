import { BinanceBalancePanel, RealAccountPanel, type AccountTrack, type BinanceBalance, type PaperBook } from "@/components/Balances";
import { DecisionTable, PositionTable } from "@/components/DecisionTable";
import { EngineStatus, Panel } from "@/components/Shell";
import { StrategyCard, type StrategyCardData } from "@/components/StrategyCard";
import { SymbolTicker } from "@/components/SymbolTicker";
import { engineFetch } from "@/lib/engine";
import { DecisionDiagnostics } from "@/components/DecisionDiagnostics";
import { ExchangeExposure, type ExchangeState } from "@/components/ExchangeExposure";

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
  positions: Array<{ position_id?: string; strategy: string; symbol: string; side: string; quantity: number; entry: number; stop: number; target: number; notional?: number; leverage?: number | null; unrealized: number; target_pnl?: number; stop_pnl?: number }>;
  binance_balance?: BinanceBalance;
  account?: AccountTrack;
  paper?: PaperBook;
  exchange?: ExchangeState;
};

export default async function OverviewPage() {
  const [overview, decisions] = await Promise.all([
    engineFetch<Overview>("/api/overview?fresh_balance=true"),
    engineFetch<{ rows: Parameters<typeof DecisionTable>[0]["rows"] }>("/api/decisions?limit=200"),
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
      <DecisionDiagnostics rows={decisions.ok ? decisions.data?.rows ?? [] : null} />
      <div className="grid gap-3 lg:grid-cols-2">
        <BinanceBalancePanel balance={data.binance_balance} track={data.account} />
        <RealAccountPanel balance={data.binance_balance} />
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
      <Panel title="POSIÇÕES DO ROBÔ · CONCILIAÇÃO BINANCE">
        <PositionTable rows={data.positions} exchange={data.exchange} />
      </Panel>
      <Panel title="CONTA BINANCE · TODAS AS EXPOSIÇÕES E DÍVIDAS"><ExchangeExposure data={data.exchange} /></Panel>
      <Panel title="LATEST DECISIONS">
        <DecisionTable rows={decisions.ok ? (decisions.data?.rows ?? []).slice(0, 12) : null} />
      </Panel>
    </div>
  );
}
