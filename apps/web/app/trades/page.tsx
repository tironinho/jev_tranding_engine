import { Empty, Panel } from "@/components/Shell";
import { engineFetch } from "@/lib/engine";
import { money, num, shortTime, signedClass } from "@/lib/utils";

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
  const result = await engineFetch<{ rows: Trade[] }>(`/api/trades?${params.toString()}`);
  const rows = result.ok ? result.data?.rows ?? [] : null;
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
      <Panel title="TRADES — NET PNL">
        {!rows ? <Empty /> : !rows.length ? <Empty /> : (
          <div className="overflow-x-auto">
            <table className="w-full text-left text-[11px]">
              <thead className="text-mute">
                <tr className="border-b border-line">
                  {["SYMBOL", "STRATEGY", "SIDE", "ENTRY", "EXIT", "STOP", "TARGET", "DURATION", "GROSS", "FEES", "SLIP", "FUND", "NET", "R"].map((head) => (
                    <th key={head} className="px-2 py-2 font-normal tracking-[0.08em]">{head}</th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {rows.map((trade) => {
                  const duration = Math.max(0, (new Date(trade.closed_at).getTime() - new Date(trade.opened_at).getTime()) / 1000);
                  return (
                    <tr key={trade.trade_id} className="border-b border-line/70 font-mono">
                      <td className="px-2 py-2">{trade.symbol}</td>
                      <td className="px-2 py-2">{trade.strategy}</td>
                      <td className="px-2 py-2">{trade.side}</td>
                      <td className="px-2 py-2">{num(trade.entry_price, 4)}</td>
                      <td className="px-2 py-2">{num(trade.exit_price, 4)}</td>
                      <td className="px-2 py-2">{num(trade.stop, 4)}</td>
                      <td className="px-2 py-2">{num(trade.target, 4)}</td>
                      <td className="px-2 py-2">{duration.toFixed(0)}s</td>
                      <td className="px-2 py-2">{money(trade.gross_pnl)}</td>
                      <td className="px-2 py-2">{num(trade.fees, 4)}</td>
                      <td className="px-2 py-2">{num(trade.slippage, 4)}</td>
                      <td className="px-2 py-2">{num(trade.funding, 4)}</td>
                      <td className={`px-2 py-2 ${signedClass(trade.net_pnl)}`}>{money(trade.net_pnl)}</td>
                      <td className="px-2 py-2">{trade.r_multiple == null ? "NO DATA" : num(trade.r_multiple, 2)}</td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
            <div className="px-2 py-2 text-[10px] text-mute">último evento {rows[0] ? shortTime(rows[0].closed_at) : "NO DATA"}</div>
          </div>
        )}
      </Panel>
    </div>
  );
}
