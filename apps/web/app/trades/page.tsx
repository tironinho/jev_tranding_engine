import { PositionTable } from "@/components/DecisionTable";
import { Empty, Panel } from "@/components/Shell";
import { engineFetch } from "@/lib/engine";
import { money, num, pct, shortTime, sidePnl, signedClass } from "@/lib/utils";

export const dynamic = "force-dynamic";

type Trade = {
  trade_id: string;
  symbol: string;
  strategy: string;
  side: string;
  entry_price: number;
  exit_price: number;
  stop: number;
  target: number;
  opened_at: string;
  closed_at: string;
  gross_pnl: number;
  fees: number;
  slippage: number;
  funding: number;
  net_pnl: number;
  quantity: number;
  r_multiple: number | null;
  quantitative_regime?: string | null;
};

export default async function TradesPage({
  searchParams,
}: {
  searchParams: Promise<Record<string, string | undefined>>;
}) {
  const query = await searchParams;
  const params = new URLSearchParams();
  for (const key of ["strategy", "symbol", "side", "result", "regime"]) {
    const value = query[key];
    if (value) params.set(key, value);
  }
  params.set("mode", "live");
  const [result, positions] = await Promise.all([
    engineFetch<{ rows: Trade[] }>(`/api/trades?${params.toString()}`),
    engineFetch<{ rows: Parameters<typeof PositionTable>[0]["rows"] }>("/api/positions"),
  ]);
  const open = (positions.ok ? positions.data?.rows ?? [] : []).filter((row) => row?.mode === "live");
  const rows = result.ok ? result.data?.rows ?? [] : null;
  const totals = rows?.reduce(
    (sum, trade) => {
      const qty = trade.quantity ?? 0;
      sum.net += trade.net_pnl;
      sum.gross += trade.gross_pnl;
      sum.notional += trade.entry_price * qty;
      sum.target += sidePnl(trade.side, trade.entry_price, trade.target, qty);
      sum.stop += sidePnl(trade.side, trade.entry_price, trade.stop, qty);
      if (trade.net_pnl > 0) {
        sum.wins += 1;
        sum.winNet += trade.net_pnl;
      } else if (trade.net_pnl < 0) {
        sum.losses += 1;
        sum.lossNet += trade.net_pnl;
      }
      if (trade.r_multiple != null) {
        sum.rSum += trade.r_multiple;
        sum.rCount += 1;
      }
      return sum;
    },
    { net: 0, gross: 0, notional: 0, target: 0, stop: 0, wins: 0, losses: 0, winNet: 0, lossNet: 0, rSum: 0, rCount: 0 },
  );
  const count = rows?.length ?? 0;
  const winRate = totals && count ? totals.wins / count : null;
  const profitFactor = totals && Math.abs(totals.lossNet) > 0 ? totals.winNet / Math.abs(totals.lossNet) : null;
  const expectancyR = totals && totals.rCount ? totals.rSum / totals.rCount : null;
  return (
    <div className="grid gap-3">
      <form className="flex flex-wrap gap-2 text-xs">
        <input name="strategy" placeholder="strategy" defaultValue={query.strategy} className="border border-line bg-ink px-2 py-1" />
        <input name="symbol" placeholder="symbol" defaultValue={query.symbol} className="border border-line bg-ink px-2 py-1" />
        <input name="side" placeholder="LONG/SHORT" defaultValue={query.side} className="border border-line bg-ink px-2 py-1" />
        <input name="result" placeholder="win/loss" defaultValue={query.result} className="border border-line bg-ink px-2 py-1" />
        <input name="regime" placeholder="regime" defaultValue={query.regime} className="border border-line bg-ink px-2 py-1" />
        <button className="border border-line px-2 py-1">filtrar</button>
      </form>
      {open.length ? (
        <Panel title="ABERTAS — PRODUÇÃO">
          <PositionTable rows={open} />
        </Panel>
      ) : null}
      <Panel title="FECHADAS — PRODUÇÃO">
        {!rows ? <Empty /> : !rows.length ? <Empty label="NENHUM TRADE DE PRODUÇÃO FECHADO" /> : (
          <div className="overflow-x-auto">
            <dl className="mb-3 grid grid-cols-2 gap-3 text-[11px] md:grid-cols-6">
              <div>
                <dt className="text-mute">TRADES</dt>
                <dd className="font-mono">{count}</dd>
              </div>
              <div>
                <dt className="text-mute">WINS</dt>
                <dd className="font-mono">{totals?.wins ?? 0}</dd>
              </div>
              <div>
                <dt className="text-mute">LOSSES</dt>
                <dd className="font-mono">{totals?.losses ?? 0}</dd>
              </div>
              <div>
                <dt className="text-mute">WIN RATE</dt>
                <dd className="font-mono">{pct(winRate)}</dd>
              </div>
              <div>
                <dt className="text-mute">PROFIT FACTOR</dt>
                <dd className="font-mono">{profitFactor == null ? "NO DATA" : num(profitFactor, 2)}</dd>
              </div>
              <div>
                <dt className="text-mute">EXPECTANCY</dt>
                <dd className={`font-mono ${signedClass(expectancyR)}`}>{expectancyR == null ? "NO DATA" : `${num(expectancyR, 2)} R`}</dd>
              </div>
            </dl>
            <dl className="mb-3 grid grid-cols-2 gap-3 text-[11px] md:grid-cols-5">
              <div>
                <dt className="text-mute">REALIZED NET</dt>
                <dd className={`font-mono ${signedClass(totals?.net)}`}>{money(totals?.net)} USDT</dd>
              </div>
              <div>
                <dt className="text-mute">GROSS</dt>
                <dd className={`font-mono ${signedClass(totals?.gross)}`}>{money(totals?.gross)}</dd>
              </div>
              <div>
                <dt className="text-mute">NOTIONAL</dt>
                <dd className="font-mono">{num(totals?.notional, 2)}</dd>
              </div>
              <div>
                <dt className="text-mute">SE O ALVO</dt>
                <dd className={`font-mono ${signedClass(totals?.target)}`}>{money(totals?.target)}</dd>
              </div>
              <div>
                <dt className="text-mute">SE O STOP</dt>
                <dd className={`font-mono ${signedClass(totals?.stop)}`}>{money(totals?.stop)}</dd>
              </div>
            </dl>
            <table className="w-full text-left text-[11px]">
              <thead className="text-mute">
                <tr className="border-b border-line">
                  {["SYMBOL", "STRATEGY", "SIDE", "QTY", "NOTIONAL", "ENTRY", "EXIT", "GROSS", "FEES", "NET", "TARGET $", "STOP $", "R"].map((head) => (
                    <th key={head} className="px-2 py-2 font-normal tracking-[0.08em]">{head}</th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {rows.map((trade) => {
                  const qty = trade.quantity ?? 0;
                  const notional = trade.entry_price * qty;
                  const target = sidePnl(trade.side, trade.entry_price, trade.target, qty);
                  const stop = sidePnl(trade.side, trade.entry_price, trade.stop, qty);
                  return (
                    <tr key={trade.trade_id} className="border-b border-line/70 font-mono">
                      <td className="px-2 py-2">{trade.symbol}</td>
                      <td className="px-2 py-2">{trade.strategy}</td>
                      <td className="px-2 py-2">{trade.side}</td>
                      <td className="px-2 py-2">{num(qty, 4)}</td>
                      <td className="px-2 py-2">{num(notional, 2)}</td>
                      <td className="px-2 py-2">{num(trade.entry_price, 4)}</td>
                      <td className="px-2 py-2">{num(trade.exit_price, 4)}</td>
                      <td className={`px-2 py-2 ${signedClass(trade.gross_pnl)}`}>{money(trade.gross_pnl)}</td>
                      <td className="px-2 py-2">{num(trade.fees, 4)}</td>
                      <td className={`px-2 py-2 ${signedClass(trade.net_pnl)}`}>{money(trade.net_pnl)}</td>
                      <td className={`px-2 py-2 ${signedClass(target)}`}>{money(target)}</td>
                      <td className={`px-2 py-2 ${signedClass(stop)}`}>{money(stop)}</td>
                      <td className={`px-2 py-2 ${signedClass(trade.r_multiple)}`}>{trade.r_multiple == null ? "NO DATA" : num(trade.r_multiple, 2)}</td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
            <div className="px-2 py-2 text-[10px] text-mute">
              Só ordens reais fechadas, já com taxa. A posição ainda aberta fica na overview. último evento {rows[0] ? shortTime(rows[0].closed_at) : "NO DATA"}
            </div>
          </div>
        )}
      </Panel>
    </div>
  );
}
