import { Panel } from "@/components/Shell";
import { money, num, signedClass } from "@/lib/utils";

export type BinanceBalance = {
  status: string;
  market_type: string;
  asset: string;
  wallet: number | null;
  available: number | null;
  unrealized: number | null;
  margin_level?: number | null;
  equity_usdt?: number | null;
  assets: Array<{ asset: string; total: number; free?: number | null }>;
  detail?: string | null;
};

export type PaperAccount = {
  strategy: string;
  starting_equity: number;
  cash: number;
  equity: number;
  unrealized: number;
  realized_today: number;
  net_pnl: number;
  open_positions: number;
};

export type PaperBook = {
  starting_equity: number;
  quote: string;
  leverage?: number;
  accounts: PaperAccount[];
};

export function BinanceBalancePanel({ balance }: { balance: BinanceBalance | null | undefined }) {
  const ready = balance?.status === "ok" && balance.wallet != null;
  const others = (balance?.assets ?? []).filter((item) => item.asset !== "USDT" && item.total !== 0);
  return (
    <Panel title={`BINANCE ${((balance?.market_type ?? "wallet").toUpperCase())} · USDT`}>
      {!balance ? (
        <div className="font-mono text-xs tracking-[0.16em] text-mute">NO DATA</div>
      ) : !ready ? (
        <div className="font-mono text-xs tracking-[0.12em] text-mute">
          {balance.status === "NO_CREDENTIALS" ? "NO DATA — SEM CHAVE" : balance.detail || balance.status}
        </div>
      ) : (
        <div className="grid gap-3">
          <div className="font-mono text-2xl text-paper">{num(balance.equity_usdt ?? balance.wallet, 2)} USDT</div>
          <dl className="grid grid-cols-2 gap-3 text-[11px]">
            <div>
              <dt className="text-mute">AVAILABLE</dt>
              <dd className="font-mono">{num(balance.available, 2)} USDT</dd>
            </div>
            {balance.unrealized != null ? (
              <div>
                <dt className="text-mute">UNREALIZED</dt>
                <dd className={`font-mono ${signedClass(balance.unrealized)}`}>{money(balance.unrealized)} USDT</dd>
              </div>
            ) : null}
            {balance.margin_level != null ? (
              <div>
                <dt className="text-mute">MARGIN LEVEL</dt>
                <dd className="font-mono">{num(balance.margin_level, 2)}</dd>
              </div>
            ) : null}
          </dl>
          {others.length ? (
            <div className="border-t border-line pt-2 font-mono text-[11px] text-mute">
              {others.map((item) => `${item.asset} ${num(item.total, 4)}`).join(" · ")}
            </div>
          ) : null}
        </div>
      )}
    </Panel>
  );
}

export function RealAccountPanel({ balance }: { balance: BinanceBalance | null | undefined }) {
  const ready = balance?.status === "ok" && balance.wallet != null;
  const rows = (balance?.assets ?? []).filter((item) => item.total !== 0 || (item.free ?? 0) !== 0);
  return (
    <Panel title="CONTA — MARGEM REAL">
      {!balance || !ready ? (
        <div className="font-mono text-xs tracking-[0.16em] text-mute">NO DATA</div>
      ) : (
        <div className="overflow-x-auto">
          <table className="w-full text-left text-[11px]">
            <thead className="text-mute">
              <tr className="border-b border-line">
                {["ASSET", "FREE", "NET"].map((head) => (
                  <th key={head} className="px-2 py-2 font-normal tracking-[0.08em]">
                    {head}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {rows.map((row) => (
                <tr key={row.asset} className="border-b border-line/70 font-mono">
                  <td className="px-2 py-2">{row.asset}</td>
                  <td className="px-2 py-2">{num(row.free, row.asset === "USDT" ? 2 : 4)}</td>
                  <td className={`px-2 py-2 ${signedClass(row.total)}`}>{num(row.total, row.asset === "USDT" ? 2 : 4)}</td>
                </tr>
              ))}
            </tbody>
          </table>
          <div className="px-2 pt-2 text-[10px] text-mute">
            Patrimônio marcado {num(balance.equity_usdt ?? balance.wallet, 2)}. USDT líquido {num(balance.wallet, 2)}. Disponível {num(balance.available, 2)}. Nível de margem {balance.margin_level == null ? "—" : num(balance.margin_level, 2)}. Uma conta só. O Jev dimensiona em cima deste patrimônio.
          </div>
        </div>
      )}
    </Panel>
  );
}

