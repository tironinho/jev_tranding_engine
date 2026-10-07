import Link from "next/link";
import { Badge, modeLabel, modeTone } from "@/components/ui/badge";

type Status = {
  engine?: string;
  trading_enabled?: boolean;
  live_armed?: boolean;
  market_type?: string;
  binance?: { status?: string };
  openai?: { status?: string };
  jev?: { status?: string; is_mock?: boolean };
  database?: { mode?: string; healthy?: boolean };
};

export function EngineStatus({ status }: { status: Status | null }) {
  if (!status) {
    return (
      <div className="border border-line bg-panel px-3 py-2 text-xs tracking-wide text-mute">
        ENGINE OFFLINE
      </div>
    );
  }
  const items = [
    ["ENGINE", status.engine === "online" ? "ONLINE" : "OFFLINE"],
    ["ENTRIES", status.trading_enabled ? "OPEN" : "STOPPED"],
    ["BINANCE", (status.binance?.status ?? "NO DATA").toUpperCase()],
    ["OPENAI", (status.openai?.status ?? "NO DATA").toUpperCase()],
    ["JEV", status.jev?.is_mock ? "MOCK" : (status.jev?.status ?? "NO DATA").toUpperCase()],
    ["DB", (status.database?.mode ?? "NO DATA").toUpperCase()],
    ["MARKET", (status.market_type ?? "NO DATA").toUpperCase()],
  ];
  return (
    <div className="flex flex-wrap items-center gap-x-4 gap-y-1 border border-line bg-panel px-3 py-2 text-[11px]">
      {items.map(([label, value]) => (
        <div key={label} className="flex items-center gap-2">
          <span className="text-mute">{label}</span>
          <span className="font-mono text-paper">{value}</span>
        </div>
      ))}
      {status.live_armed ? <Badge tone="live">LIVE ARMED</Badge> : <Badge tone="off">LIVE LOCKED</Badge>}
    </div>
  );
}

export function Shell({ children }: { children: React.ReactNode }) {
  const links = [
    ["/", "Overview"],
    ["/market", "Market"],
    ["/decisions", "Decisions"],
    ["/trades", "Trades"],
    ["/performance", "Performance"],
    ["/evolution", "Evolution"],
    ["/settings", "Settings"],
  ];
  return (
    <div className="min-h-screen">
      <div className="grid min-h-screen grid-cols-1 md:grid-cols-[180px_1fr]">
        <aside className="border-b border-line md:border-b-0 md:border-r">
          <div className="px-4 py-4">
            <div className="text-[11px] tracking-[0.22em] text-mute">RESEARCH</div>
            <div className="mt-1 text-sm">Trading Engine</div>
          </div>
          <nav className="flex gap-1 overflow-x-auto px-2 pb-3 md:block md:px-2">
            {links.map(([href, label]) => (
              <Link key={href} href={href} className="block px-2 py-1.5 text-xs text-mute hover:bg-[#1a2128] hover:text-paper">
                {label}
              </Link>
            ))}
          </nav>
        </aside>
        <main className="min-w-0 px-4 py-4 md:px-6">{children}</main>
      </div>
    </div>
  );
}

export function Panel({ title, children, aside }: { title: string; children: React.ReactNode; aside?: React.ReactNode }) {
  return (
    <section className="border border-line bg-panel">
      <header className="flex items-center justify-between border-b border-line px-3 py-2">
        <h2 className="text-[11px] tracking-[0.16em] text-mute">{title}</h2>
        {aside}
      </header>
      <div className="p-3">{children}</div>
    </section>
  );
}

export function Empty({ label = "NO DATA" }: { label?: string }) {
  return <div className="py-8 text-center font-mono text-xs tracking-[0.16em] text-mute">{label}</div>;
}

export function ModeBadge({ mode, liveArmed = false }: { mode: string; liveArmed?: boolean }) {
  return <Badge tone={modeToneSafe(mode, liveArmed)}>{modeLabel(mode, liveArmed)}</Badge>;
}

function modeToneSafe(mode: string, liveArmed: boolean) {
  return modeTone(mode, liveArmed);
}
