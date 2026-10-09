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

export type AccountTrack = {
  started: number | null;
  started_at: string | null;
  current: number | null;
  change: number | null;
  points: Array<{ t: string; equity: number }>;
};

function openedAt(value: string | null | undefined): string {
  if (!value) return "—";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "—";
  return date.toLocaleString("pt-BR", {
    timeZone: "America/Sao_Paulo",
    day: "2-digit",
    month: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  });
}

function Sparkline({ points }: { points: Array<{ equity: number }> }) {
  const step = Math.max(1, Math.ceil(points.length / 160));
  const sampled = points.filter((point, index) => index % step === 0 || index === points.length - 1);
  if (sampled.length < 2) return null;
  const values = sampled.map((point) => point.equity);
  const low = Math.min(...values);
  const high = Math.max(...values);
  const span = high - low || 1;
  const width = 320;
  const height = 56;
  const drawn = values.map((value, index) => {
    const x = (index / (values.length - 1)) * width;
    const y = height - 4 - ((value - low) / span) * (height - 8);
    return `${x.toFixed(1)},${y.toFixed(1)}`;
  });
  const up = values[values.length - 1] >= values[0];
  return (
    <svg viewBox={`0 0 ${width} ${height}`} className="h-14 w-full" role="img" aria-label="evolução do saldo">
      <polyline fill="none" stroke={up ? "#3ddc97" : "#ff6b6b"} strokeWidth="1.6" points={drawn.join(" ")} />
    </svg>
  );
}

export function AccountEvolution({ track }: { track: AccountTrack | null | undefined }) {
  const started = track?.started ?? null;
  const change = track?.change ?? null;
  const ratio = started ? (change ?? 0) / started : null;
  if (started == null) {
    return <div className="font-mono text-xs tracking-[0.16em] text-mute">SEM HISTÓRICO</div>;
  }
  return (
    <div className="grid gap-2">
      <dl className="grid grid-cols-2 gap-3 text-[11px]">
        <div>
          <dt className="text-mute">COMEÇOU</dt>
          <dd className="font-mono text-paper">{num(started, 2)} USDT</dd>
          <dd className="font-mono text-[10px] text-mute">{openedAt(track?.started_at)}</dd>
        </div>
        <div>
          <dt className="text-mute">EVOLUÇÃO</dt>
          <dd className={`font-mono ${signedClass(change)}`}>{money(change)} USDT</dd>
          <dd className={`font-mono text-[10px] ${signedClass(change)}`}>{ratio == null ? "—" : `${(ratio * 100).toFixed(2)}%`}</dd>
        </div>
      </dl>
      <Sparkline points={track?.points ?? []} />
    </div>
  );
}

function held(item: { total: number; free?: number | null }) {
  return item.total !== 0 || (item.free ?? 0) !== 0;
}

export function BinanceBalancePanel({ balance, track }: { balance: BinanceBalance | null | undefined; track?: AccountTrack | null }) {
  const ready = balance?.status === "ok" && balance.wallet != null;
  const others = (balance?.assets ?? []).filter((item) => item.asset !== "USDT" && held(item));
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
          <AccountEvolution track={track} />
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
              {others.map((item) => `${item.asset} ${num(item.total, 8)}`).join(" · ")}
            </div>
          ) : null}
        </div>
      )}
    </Panel>
  );
}

export function RealAccountPanel({ balance }: { balance: BinanceBalance | null | undefined }) {
  const ready = balance?.status === "ok" && balance.wallet != null;
  const rows = (balance?.assets ?? []).filter(held);
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
                  <td className="px-2 py-2">{num(row.free, row.asset === "USDT" ? 2 : 8)}</td>
                  <td className={`px-2 py-2 ${signedClass(row.total)}`}>{num(row.total, row.asset === "USDT" ? 2 : 8)}</td>
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

