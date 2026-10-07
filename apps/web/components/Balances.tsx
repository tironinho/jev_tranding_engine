import { Panel } from "@/components/Shell";
import { money, num, signedClass } from "@/lib/utils";

export type BinanceBalance = {
  status: string;
  market_type: string;
  asset: string;
  wallet: number | null;
  available: number | null;
  unrealized: number | null;
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
  accounts: PaperAccount[];
};

const LABELS: Record<string, string> = {
  baseline: "BASELINE",
  baseline_jev: "BASELINE + JEV",
  baseline_openai_jev: "BASELINE + OPENAI + JEV",
};

export function BinanceBalancePanel({ balance }: { balance: BinanceBalance | null | undefined }) {
  const ready = balance?.status === "ok" && balance.wallet != null;
  const others = (balance?.assets ?? []).filter((item) => item.asset !== "USDT" && item.total > 0);
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
          <div className="font-mono text-2xl text-paper">{num(balance.wallet, 2)} USDT</div>
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

export function PaperBookPanel({ book }: { book: PaperBook | null | undefined }) {
  const rows = book?.accounts ?? [];
  return (
    <Panel title="SIMULADO — MESMA BANCA EM CADA ESTRATÉGIA">
      {!rows.length ? (
        <div className="font-mono text-xs tracking-[0.16em] text-mute">NO DATA</div>
      ) : (
        <div className="overflow-x-auto">
          <table className="w-full text-left text-[11px]">
            <thead className="text-mute">
              <tr className="border-b border-line">
                {["STRATEGY", "START", "CASH", "EQUITY", "OPEN", "TODAY", "VS START"].map((head) => (
                  <th key={head} className="px-2 py-2 font-normal tracking-[0.08em]">
                    {head}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {rows.map((row) => (
                <tr key={row.strategy} className="border-b border-line/70 font-mono">
                  <td className="px-2 py-2">{LABELS[row.strategy] ?? row.strategy}</td>
                  <td className="px-2 py-2">{num(row.starting_equity, 2)}</td>
                  <td className="px-2 py-2">{num(row.cash, 2)}</td>
                  <td className="px-2 py-2">{num(row.equity, 2)}</td>
                  <td className={`px-2 py-2 ${signedClass(row.unrealized)}`}>{money(row.unrealized)}</td>
                  <td className={`px-2 py-2 ${signedClass(row.realized_today)}`}>{money(row.realized_today)}</td>
                  <td className={`px-2 py-2 ${signedClass(row.net_pnl)}`}>{money(row.net_pnl)}</td>
                </tr>
              ))}
            </tbody>
          </table>
          <div className="px-2 pt-2 text-[10px] text-mute">
            USDT simulado. Não some as três contas. OPEN é a posição marcada. VS START é equity menos a banca inicial.
          </div>
        </div>
      )}
    </Panel>
  );
}
