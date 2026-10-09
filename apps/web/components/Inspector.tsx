import { Empty } from "@/components/Shell";
import { CandidatePlans, type CandidatePlan } from "@/components/CandidatePlans";
import { num, pct, shortTime } from "@/lib/utils";

export function DecisionInspector({ payload }: { payload: Record<string, unknown> | null }) {
  if (!payload) return <Empty />;
  const decision = payload.decision as { timestamp?: string; action?: string; reason_codes?: string[]; metadata?: { candidate_plans?: CandidatePlan[]; selected_plan_id?: string; jev_continuation?: number } } | undefined;
  const risk = payload.risk as { accepted?: boolean; reject_reasons?: string[]; economics?: { net_rr?: number; net_risk?: number; net_reward?: number } } | undefined;
  const snapshot = payload.snapshot as { timestamp?: string } | undefined;
  const orders = payload.orders as Array<{ status?: string; exchange_at?: string; received_at?: string }> | undefined;
  return (
    <div className="grid gap-3">
      <section className="border border-line bg-panel p-3 text-xs leading-6">
        <p>Snapshot {shortTime(snapshot?.timestamp)} → decisão {shortTime(decision?.timestamp)} (Brasília): {decision?.action} · {decision?.reason_codes?.join(" · ")}</p>
        <p>Risco: {risk ? risk.accepted ? "Aprovado" : risk.reject_reasons?.join(" · ") : "Sem avaliação registrada"}. R:R líquido {num(risk?.economics?.net_rr)} · perda planejada {num(risk?.economics?.net_risk,4)} · ganho planejado {num(risk?.economics?.net_reward,4)} USDT.</p>
        <p>p(alvo) Jev: {pct(decision?.metadata?.jev_continuation)} — estimativa não calibrada.</p>
        <p>Execução: {orders?.length ? orders.map(o => `${o.status} · bolsa ${shortTime(o.exchange_at)} · recebido ${shortTime(o.received_at)}`).join(" / ") : "Sem ordem registrada"}.</p>
        <CandidatePlans plans={decision?.metadata?.candidate_plans} selected={decision?.metadata?.selected_plan_id} />
        <p className="mt-2 text-mute">Future labels medem movimentos posteriores; não comprovam que um alvo foi executado. Ordens, fills e trade abaixo registram a execução. Alvo e stop na mesma vela exigem dados intravela.</p>
      </section>
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
    <details className="border border-line bg-panel">
      <summary className="cursor-pointer border-b border-line px-3 py-2 text-[11px] tracking-[0.14em] text-mute">{title}</summary>
      <pre className="overflow-x-auto p-3 font-mono text-[11px] leading-5 text-[#c5d0d8]">
        {value == null ? "NO DATA" : JSON.stringify(value, null, 2)}
      </pre>
    </details>
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
          <span className="text-paper">{shortTime(row.timestamp)}</span> {row.kind} {row.message}
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
