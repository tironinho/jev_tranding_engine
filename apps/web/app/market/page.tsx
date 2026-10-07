import { MarketStatePanel } from "@/components/Inspector";
import { Panel } from "@/components/Shell";
import { SymbolTicker } from "@/components/SymbolTicker";
import { engineFetch } from "@/lib/engine";

export const dynamic = "force-dynamic";

type Market = {
  market_type: string;
  symbols: Array<{ symbol: string; price: number | null; spread_bps?: number | null; funding_rate?: number | null; open_interest?: number | null; status?: string; stale?: boolean }>;
};

export default async function MarketPage() {
  const market = await engineFetch<Market>("/api/market");
  if (!market.ok || !market.data) {
    return <Panel title="MARKET"><div className="font-mono text-xs text-mute">NO DATA</div></Panel>;
  }
  const details = await Promise.all(
    market.data.symbols.map(async (ticker) => {
      const detail = await engineFetch<{ ticker: Market["symbols"][number]; snapshot: { features?: Record<string, unknown>; quantitative_regime?: string; price?: number } | null }>(
        `/api/market/${ticker.symbol}`,
      );
      return { ticker, snapshot: detail.data?.snapshot ?? null };
    }),
  );
  return (
    <div className="grid gap-4">
      <div className="text-[11px] tracking-[0.16em] text-mute">MARKET TYPE {market.data.market_type.toUpperCase()}</div>
      {details.map((item) => (
        <div key={item.ticker.symbol} className="grid gap-3 lg:grid-cols-[240px_1fr]">
          <SymbolTicker ticker={item.ticker} />
          <Panel title={`${item.ticker.symbol} SNAPSHOT`}>
            <MarketStatePanel snapshot={item.snapshot} />
          </Panel>
        </div>
      ))}
    </div>
  );
}
