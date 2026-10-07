import { ModeBadge } from "@/components/Shell";
import { money, num, pct, signedClass } from "@/lib/utils";

export type StrategyCardData = {
  key: string;
  label: string;
  mode: string;
  enabled: boolean;
  trades: number;
  win_rate: number | null;
  profit_factor: number | null;
  expectancy_r: number | null;
  net_pnl: number;
  max_drawdown: number | null;
  equity: number;
};

export function StrategyCard({ card, liveArmed }: { card: StrategyCardData; liveArmed: boolean }) {
  const mode = card.enabled ? card.mode : "off";
  const hasTrades = card.trades > 0;
  return (
    <article className="border border-line bg-panel p-3">
      <div className="flex items-start justify-between gap-3">
        <h3 className="text-xs tracking-[0.14em]">{card.label}</h3>
        <ModeBadge mode={mode} liveArmed={liveArmed} />
      </div>
      <dl className="mt-3 grid grid-cols-2 gap-x-3 gap-y-2 text-[11px]">
        <Stat label="TRADES" value={String(card.trades)} />
        <Stat label="WIN RATE" value={hasTrades ? pct(card.win_rate) : "NO DATA"} />
        <Stat label="PROFIT FACTOR" value={hasTrades ? num(card.profit_factor, 2) : "NO DATA"} />
        <Stat label="EXPECTANCY" value={hasTrades ? `${num(card.expectancy_r, 2)} R` : "NO DATA"} />
        <Stat label="NET PNL" value={hasTrades ? money(card.net_pnl) : "NO DATA"} className={hasTrades ? signedClass(card.net_pnl) : ""} />
        <Stat label="DRAWDOWN" value={hasTrades ? pct(card.max_drawdown) : "NO DATA"} />
      </dl>
      <div className="mt-3 border-t border-line pt-2 font-mono text-[11px] text-mute">
        simulado {num(card.equity, 2)} USDT
      </div>
    </article>
  );
}

function Stat({ label, value, className }: { label: string; value: string; className?: string }) {
  return (
    <div>
      <dt className="text-mute">{label}</dt>
      <dd className={`font-mono text-paper ${className ?? ""}`}>{value}</dd>
    </div>
  );
}
