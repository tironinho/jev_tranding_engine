export function SymbolTicker({
  ticker,
}: {
  ticker: {
    symbol: string;
    price: number | null;
    spread_bps?: number | null;
    funding_rate?: number | null;
    open_interest?: number | null;
    status?: string;
    stale?: boolean;
  };
}) {
  return (
    <div className="border border-line bg-panel px-3 py-3">
      <div className="flex items-center justify-between text-[11px] text-mute">
        <span>{ticker.symbol}</span>
        <span>{ticker.price === null ? "NO DATA" : ticker.stale ? "STALE" : ticker.status ?? "LIVE"}</span>
      </div>
      <div className="mt-2 font-mono text-lg">{ticker.price === null ? "NO DATA" : ticker.price.toLocaleString("en-US")}</div>
      <div className="mt-2 grid grid-cols-3 gap-2 font-mono text-[10px] text-mute">
        <span>spr {ticker.spread_bps == null ? "—" : ticker.spread_bps.toFixed(2)}</span>
        <span>fund {ticker.funding_rate == null ? "—" : ticker.funding_rate.toFixed(5)}</span>
        <span>oi {ticker.open_interest == null ? "—" : ticker.open_interest.toLocaleString("en-US")}</span>
      </div>
    </div>
  );
}
