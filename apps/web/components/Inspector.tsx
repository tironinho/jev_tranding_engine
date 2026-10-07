import { Empty } from "@/components/Shell";

export function DecisionInspector({ payload }: { payload: Record<string, unknown> | null }) {
  if (!payload) return <Empty />;
  return (
    <div className="grid gap-3">
      <Block title="Decisão" value={payload.decision} />
      <Block title="Market snapshot" value={payload.snapshot} />
      <Block title="Features" value={payload.features} />
      <Block title="OpenAI / Jev" value={payload.artifacts} />
      <Block title="Risk" value={payload.risk} />
      <Block title="Ordens e fills" value={{ orders: payload.orders, fills: payload.fills }} />
      <Block title="Trade" value={payload.trade} />
      <Block title="Consensus" value={payload.consensus} />
      <Block title="Future labels" value={payload.labels} />
    </div>
  );
}

function Block({ title, value }: { title: string; value: unknown }) {
  return (
    <section className="border border-line bg-panel">
      <header className="border-b border-line px-3 py-2 text-[11px] tracking-[0.14em] text-mute">{title}</header>
      <pre className="overflow-x-auto p-3 font-mono text-[11px] leading-5 text-[#c5d0d8]">
        {value == null ? "NO DATA" : JSON.stringify(value, null, 2)}
      </pre>
    </section>
  );
}

export function MarketStatePanel({
  snapshot,
}: {
  snapshot: { features?: Record<string, unknown>; quantitative_regime?: string; price?: number } | null;
}) {
  if (!snapshot?.features) return <Empty />;
  const keys = [
    "return_1m",
    "return_5m",
    "ema_alignment",
    "distance_to_vwap",
    "rsi",
    "atr_normalized",
    "volume_ratio",
    "cvd",
    "imbalance_10",
    "spread_bps",
    "funding_rate",
    "open_interest",
    "breakout",
    "range_position",
  ];
  return (
    <div>
      <div className="mb-2 text-[11px] text-mute">regime {snapshot.quantitative_regime ?? "NO DATA"}</div>
      <dl className="grid grid-cols-2 gap-2 text-[12px] md:grid-cols-3">
        {keys.map((key) => (
          <div key={key} className="border-b border-line/50 py-1">
            <dt className="text-mute">{key}</dt>
            <dd className="font-mono">{formatFeature(snapshot.features?.[key])}</dd>
          </div>
        ))}
      </dl>
    </div>
  );
}

function formatFeature(value: unknown) {
  if (value === null || value === undefined) return "NO DATA";
  if (typeof value === "number") return value.toPrecision(4);
  return String(value);
}

export function EngineLogs({ rows }: { rows: Array<{ timestamp?: string; kind?: string; message?: string }> | null }) {
  if (!rows?.length) return <Empty />;
  return (
    <ul className="max-h-80 space-y-1 overflow-auto font-mono text-[11px]">
      {rows.map((row, index) => (
        <li key={`${row.timestamp}-${index}`} className="text-mute">
          <span className="text-paper">{row.timestamp?.slice(11, 19) ?? ""}</span> {row.kind} {row.message}
        </li>
      ))}
    </ul>
  );
}

export function RiskPanel({
  limits,
}: {
  limits: Record<string, number | string> | null;
}) {
  if (!limits) return <Empty />;
  return (
    <dl className="grid grid-cols-2 gap-2 text-[12px]">
      {Object.entries(limits).map(([key, value]) => (
        <div key={key} className="border-b border-line/50 py-1">
          <dt className="text-mute">{key}</dt>
          <dd className="font-mono">{String(value)}</dd>
        </div>
      ))}
    </dl>
  );
}
