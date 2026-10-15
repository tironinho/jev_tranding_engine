import Link from "next/link";
import { CandidatePlans, type CandidatePlan } from "@/components/CandidatePlans";
import { Empty, Panel } from "@/components/Shell";
import { pct, shortTime, signedClass } from "@/lib/utils";

export type JevReview = {
  candidate_plans?: CandidatePlan[];
  selected_plan_id?: string;
  decision_id: string;
  symbol?: string;
  timestamp?: string;
  action?: string;
  jev_action?: string;
  confidence?: number;
  baseline_score?: number | null;
  effect?: string;
  required_continuation?: number | null;
  size_scale?: number | null;
  reason_codes?: string[];
  state?: {
    symbol?: string;
    baseline_action?: string;
    candidate_side?: string;
    candidate_action?: string;
    baseline_confidence?: number;
    baseline_scores?: Record<string, number | null>;
    taker_flow_1m?: number | null;
    return_60m?: number | null;
    range_60m?: number | null;
    breakout?: boolean | null;
    breakdown?: boolean | null;
    intelligence?: Record<string, unknown> | null;
    evidence?: SimpleEvidence;
    trade?: ObservationTrade;
  };
  response?: {
    trend_continuation_probability?: number | null;
    reversal_probability?: number | null;
    false_breakout_probability?: number | null;
    model?: string | null;
    latency_ms?: number | null;
    is_mock?: boolean | null;
    error?: string | null;
  };
};

type SimpleEvidence = {
  trend?: {
    ema_alignment?: number; ema20_slope_5m_pct?: number;
    price_vs_ema20_15m_pct?: number; context_15m_slope_pct?: number; distance_to_vwap_pct?: number;
  };
  momentum?: { rsi_14?: number; roc_10m_pct?: number; return_60m_pct?: number };
  volume?: { relative_to_previous_20m?: number };
  flow?: { taker_1m?: number; taker_5m?: number };
  structure?: { event?: string; extension_from_break_level_atr?: number };
  volatility?: { atr_14_pct?: number; range_60m_pct?: number };
  liquidity?: { spread_bps?: number };
};

type ObservationTrade = {
  target_distance_pct?: number;
  stop_distance_pct?: number;
  time_exit?: { rule?: string; after_minutes?: number; estimated_breakeven_distance_pct?: number };
};

export type JevExecution = {
  risk?: { accepted?: boolean; reject_reasons?: string[] } | null;
  orders?: Array<{ status?: string }>;
  fills?: Array<Record<string, unknown>>;
  trade?: Record<string, unknown> | null;
};

const EFFECTS: Record<string, string> = {
  confirm: "CONFIRMOU",
  veto: "VETOU",
  assessed: "SÓ LEU",
  fallback: "BASELINE",
  idle: "NÃO CHAMADO",
  not_called: "SEM PLANO",
  shadow_confirm: "SOMBRA · APROVARIA",
  shadow_veto: "SOMBRA · VETOU",
  shadow_unavailable: "SOMBRA · ERRO",
  shadow_deferred: "SOMBRA · ADIADA",
};

const SCORES: Array<[string, string]> = [
  ["trend_score", "TENDÊNCIA"],
  ["context_trend_score", "TENDÊNCIA 5M/15M"],
  ["momentum_score", "MOMENTO"],
  ["volume_score", "VOLUME"],
  ["orderflow_score", "FLUXO"],
  ["structure_score", "ESTRUTURA"],
  ["volatility_score", "VOLATILIDADE"],
  ["liquidity_score", "LIQUIDEZ"],
];

export function JevDesk({
  rows,
  selected,
  history,
  execution,
}: {
  rows: JevReview[] | null;
  selected: JevReview | null;
  history?: "postgres" | "memory" | "unavailable";
  execution?: JevExecution | null;
}) {
  if (!rows) return <Panel title="JEV"><Empty label="ENGINE OFFLINE" /></Panel>;
  if (!rows.length) {
    const label = history === "unavailable" ? "HISTÓRICO INDISPONÍVEL" : "AINDA SEM LEITURA";
    return <Panel title="JEV"><Empty label={label} /></Panel>;
  }
  return (
    <div className="grid gap-4 lg:grid-cols-[280px_1fr]">
      <Panel title="LEITURAS">
        <ul className="max-h-[70vh] space-y-1 overflow-auto">
          {rows.map((row) => {
            const active = row.decision_id === selected?.decision_id;
            return (
              <li key={row.decision_id}>
                <Link
                  href={`/jev?id=${row.decision_id}`}
                  className={`block border px-2 py-2 ${active ? "border-[#3a4650] bg-[#141a20]" : "border-transparent hover:bg-[#141a20]"}`}
                >
                  <div className="flex items-center justify-between text-[11px]">
                    <span className="font-mono">{row.symbol ?? "NO DATA"}</span>
                    <span className="font-mono text-mute">{shortTime(row.timestamp)}</span>
                  </div>
                  <div className="mt-1 flex items-center justify-between text-[11px]">
                    <span className="text-mute">{row.state?.baseline_action ?? row.state?.candidate_side ?? "—"}</span>
                    <span className={effectClass(row.effect, row.response?.error)}>{effectText(row.effect, row.response?.error)}</span>
                  </div>
                </Link>
              </li>
            );
          })}
        </ul>
      </Panel>
      {selected ? <Reading review={selected} execution={execution} /> : <Empty />}
    </div>
  );
}

function Reading({ review, execution }: { review: JevReview; execution?: JevExecution | null }) {
  const state = review.state ?? {};
  const answer = review.response ?? {};
  const required = review.required_continuation;
  const simple = state.evidence != null;
  const event = state.evidence?.structure?.event;
  const hasBreak = simple ? event === "BREAKOUT" || event === "BREAKDOWN" : state.breakout || state.breakdown;
  const score = review.baseline_score ?? state.baseline_confidence;
  return (
    <div className="grid gap-4">
      <Panel
        title={`${review.symbol ?? "NO DATA"} · ${shortTime(review.timestamp)}`}
        aside={<span className={`font-mono text-[11px] ${effectClass(review.effect, answer.error)}`}>{effectText(review.effect, answer.error)}</span>}
      >
        <div className="flex flex-wrap items-end justify-between gap-3">
          <div>
            <div className="text-[11px] text-mute">BASELINE PEDIU</div>
            <div className="font-mono text-2xl">{state.baseline_action ?? state.candidate_side ?? "NO DATA"}</div>
            <div className="font-mono text-[11px] text-mute">
              {score == null ? "Score não registrado" : `score ${pct(score)} · não é probabilidade`}
              {simple ? " · não enviado ao JEV" : ""}
              {state.candidate_action ? ` · candidato sombra ${state.candidate_action}` : ""}
            </div>
          </div>
          <div className="text-right">
            <div className="text-[11px] text-mute">JEV DEVOLVEU</div>
            <div className="font-mono text-2xl">{review.jev_action ?? review.action ?? "NO DATA"}</div>
            <div className="font-mono text-[11px] text-mute">
              {answer.is_mock ? "MOCK" : answer.model ?? "NO DATA"}
              {answer.latency_ms != null ? ` · ${Math.round(answer.latency_ms)} ms` : ""}
            </div>
          </div>
        </div>
      </Panel>
      <ExecutionStatus review={review} execution={execution} />
      <Panel title={simple ? "OPERAÇÃO AVALIADA" : "PLANOS E EXPECTATIVA LÍQUIDA"}>
        {simple ? <ObservationPlan trade={state.trade} /> : <CandidatePlans plans={review.candidate_plans} selected={review.selected_plan_id} />}
        <Link className="mt-3 block text-xs underline" href={`/decisions/${review.decision_id}`}>Ver decisão, custos, ordens e resultado observado</Link>
      </Panel>
      <div className="grid gap-4 lg:grid-cols-2">
        <Panel title="O QUE ENTROU">
          {simple ? <ExchangeEvidence evidence={state.evidence!} /> : <div className="grid gap-3">
            {SCORES.map(([key, label]) => (
              <SignedRow key={key} label={label} value={state.baseline_scores?.[key] ?? null} />
            ))}
            <SignedRow label="TAKER 1M" value={state.taker_flow_1m ?? null} />
            <SignedRow label="RETORNO 60M" value={state.return_60m ?? null} />
            <div className="flex gap-4 font-mono text-[11px] text-mute">
              <span>ROMPIMENTO {flag(state.breakout)}</span>
              <span>PERDA {flag(state.breakdown)}</span>
              <span>FAIXA 60M {state.range_60m == null ? "sem dado" : pct(state.range_60m)}</span>
            </div>
          </div>}
        </Panel>
        <Panel title="O QUE VOLTOU">
          <div className="grid gap-4">
            <ProbRow label="CONTINUAÇÃO" value={answer.trend_continuation_probability ?? null} mark={required ?? null} />
            <ProbRow label="REVERSÃO" value={answer.reversal_probability ?? null} />
            <ProbRow label="FALSO ROMPIMENTO" value={answer.false_breakout_probability ?? null} />
            <p className="text-[11px] text-mute">
              {required == null
                ? "O corte de continuação não foi gravado nesta leitura."
                : hasBreak
                  ? `Rompimento identificado. Piso de referência: ${pct(required)}.${review.size_scale != null && review.size_scale < 1 ? ` Tamanho reduzido para ${pct(review.size_scale)}.` : ""} A entrada ainda depende do risco.`
                  : `Sem rompimento, confirmar exige continuação de pelo menos ${pct(required)}.`}
            </p>
            {answer.error ? <p className="font-mono text-[11px] text-[#ff6b6b]">{answer.error}</p> : null}
          </div>
        </Panel>
      </div>
      {!simple ? <IntelligenceBlock intelligence={state.intelligence ?? null} /> : null}
    </div>
  );
}

function ExecutionStatus({ review, execution }: { review: JevReview; execution?: JevExecution | null }) {
  if (review.action === "NO_TRADE") return null;
  if (!execution) {
    return <Panel title="EXECUÇÃO"><p className="font-mono text-[11px] text-mute">RESULTADO DA EXECUÇÃO INDISPONÍVEL</p></Panel>;
  }
  const risk = execution.risk;
  const orders = execution.orders ?? [];
  const fills = execution.fills ?? [];
  let status = "AGUARDANDO AVALIAÇÃO DE RISCO";
  let detail = "O JEV confirmou o plano, mas ainda não há avaliação de risco registrada.";
  let color = "text-mute";
  if (risk?.accepted === false) {
    status = "BLOQUEADO PELO RISCO";
    detail = risk.reject_reasons?.join(" · ") || "Motivo não registrado";
    if (risk.reject_reasons?.includes("MAX_CONSECUTIVE_LIVE_LOSSES")) {
      detail = "Limite diário de perdas consecutivas atingido. O JEV confirmou, mas a pausa de risco impediu o envio da ordem.";
    }
    color = "text-[#ff6b6b]";
  } else if (fills.length) {
    status = execution.trade ? "TRADE REGISTRADA" : "ORDEM EXECUTADA";
    detail = `${fills.length} fill(s) registrado(s).`;
    color = "text-[#00f5a0]";
  } else if (orders.length) {
    status = "ORDEM SEM FILL";
    detail = orders.map((order) => order.status ?? "status desconhecido").join(" · ");
    color = "text-[#f3c969]";
  } else if (risk?.accepted) {
    status = "RISCO APROVADO, SEM ORDEM";
    detail = "A execução não registrou ordem; verifique bloqueio live, expiração ou reconciliação.";
    color = "text-[#f3c969]";
  }
  return <Panel title="EXECUÇÃO" aside={<span className={`font-mono text-[11px] ${color}`}>{status}</span>}><p className="font-mono text-[11px] text-mute">{detail}</p></Panel>;
}

function observationNumber(value: number | undefined, digits = 4, unit = "") {
  return value != null && Number.isFinite(value) ? `${value.toFixed(digits)}${unit}` : "sem dado";
}

function ExchangeEvidence({ evidence: e }: { evidence: SimpleEvidence }) {
  const event = e.structure?.event;
  const eventLabel = event === "BREAKOUT" ? "Rompimento de resistência"
    : event === "BREAKDOWN" ? "Perda de suporte"
      : event === "NONE" ? "Sem rompimento" : "Evento não identificado";
  const groups = [
    ["TENDÊNCIA", `Médias ${observationNumber(e.trend?.ema_alignment, 2)} · EMA20 em 5m ${observationNumber(e.trend?.ema20_slope_5m_pct, 4, "%")}`],
    ["MOMENTO", `RSI ${observationNumber(e.momentum?.rsi_14, 2)} · ROC 10m ${observationNumber(e.momentum?.roc_10m_pct, 4, "%")}`],
    ["VOLUME", `${observationNumber(e.volume?.relative_to_previous_20m, 2, "x")} a média anterior de 20m`],
    ["FLUXO", `Taker 1m ${observationNumber(e.flow?.taker_1m)} · 5m ${observationNumber(e.flow?.taker_5m)}`],
    ["CONTEXTO 15M", `Inclinação ${observationNumber(e.trend?.context_15m_slope_pct, 4, "%")} · Preço vs EMA20 ${observationNumber(e.trend?.price_vs_ema20_15m_pct, 4, "%")} · VWAP ${observationNumber(e.trend?.distance_to_vwap_pct, 4, "%")}`],
    ["RETORNO 60M", observationNumber(e.momentum?.return_60m_pct, 4, "%")],
    ["ESTRUTURA", `${eventLabel}${event === "BREAKOUT" || event === "BREAKDOWN" ? ` · distância do nível ${observationNumber(e.structure?.extension_from_break_level_atr, 2, " ATR")}` : ""}`],
    ["VOLATILIDADE", `ATR ${observationNumber(e.volatility?.atr_14_pct, 4, "%")} · Faixa 60m ${observationNumber(e.volatility?.range_60m_pct, 4, "%")}`],
    ["LIQUIDEZ", `Spread ${observationNumber(e.liquidity?.spread_bps, 4, " bps")}`],
  ];
  return <div className="grid gap-3">
    {groups.map(([label, value]) => <div key={label} className="border-b border-line pb-2 text-[11px]">
      <div className="mb-1 text-mute">{label}</div>
      <div className="font-mono">{value}</div>
    </div>)}
    <p className="text-[11px] text-mute">Fluxo positivo indica compras agressoras; negativo indica vendas.</p>
  </div>;
}

function ObservationPlan({ trade }: { trade?: ObservationTrade }) {
  if (!trade) return <p className="text-xs text-mute">Descrição da operação não registrada.</p>;
  const time = trade.time_exit;
  const minutes = observationNumber(time?.after_minutes, 0);
  const rule = time?.rule === "EXIT_IF_NET_PNL_NON_POSITIVE"
    ? `Após ${minutes} min, fecha posições sem lucro líquido; vencedoras seguem para alvo ou stop.`
    : time?.rule === "EXIT_AT_DEADLINE"
      ? `Fecha após ${minutes} min, inclusive posições lucrativas.`
      : time?.rule === "TARGET_OR_STOP"
        ? "Saída por alvo ou stop, sem prazo temporal."
        : time?.rule === "EXIT_UNLESS_NET_POSITIVE_WITH_BREAKEVEN_STOP"
          ? `Após ${minutes} min, continua apenas com lucro líquido e stop no ponto de equilíbrio.`
          : "Regra temporal não registrada.";
  return <div className="space-y-2 text-xs">
    <div className="flex flex-wrap gap-x-5 gap-y-2 font-mono">
      <span>ALVO {observationNumber(trade.target_distance_pct, 4, "%")}</span>
      <span>STOP {observationNumber(trade.stop_distance_pct, 4, "%")}</span>
      {time?.estimated_breakeven_distance_pct != null ? <span>EQUILÍBRIO EST. {observationNumber(time.estimated_breakeven_distance_pct, 4, "%")}</span> : null}
    </div>
    <p className="text-mute">{rule}</p>
    <p className="text-[11px] text-mute">Distâncias estimadas a partir da cotação desta leitura.</p>
  </div>;
}

function IntelligenceBlock({ intelligence }: { intelligence: Record<string, unknown> | null }) {
  if (!intelligence) {
    return (
      <Panel title="CONTEXTO EXTERNO">
        <p className="text-[11px] text-mute">Esta leitura foi sem o snapshot de inteligência. O Jev viu só o estado curto do baseline.</p>
      </Panel>
    );
  }
  const groups: Array<[string, string]> = [
    ["microstructure", "MICROESTRUTURA"],
    ["derivatives", "DERIVATIVOS"],
    ["sentiment", "SENTIMENTO"],
    ["relative_strength", "FORÇA RELATIVA"],
    ["global", "GLOBAL"],
    ["onchain", "ON-CHAIN"],
  ];
  const quality = intelligence.data_quality as { overall?: number; missing?: string[] } | undefined;
  return (
    <Panel title="CONTEXTO EXTERNO" aside={<span className="font-mono text-[11px] text-mute">qualidade {pct(quality?.overall ?? null)}</span>}>
      <div className="grid gap-4 md:grid-cols-2">
        {groups.map(([key, label]) => (
          <section key={key}>
            <h3 className="mb-2 text-[11px] tracking-[0.14em] text-mute">{label}</h3>
            <Fields value={intelligence[key]} />
          </section>
        ))}
      </div>
      {quality?.missing?.length ? (
        <p className="mt-3 text-[11px] text-mute">Sem dado: {quality.missing.join(" · ")}</p>
      ) : null}
    </Panel>
  );
}

function Fields({ value }: { value: unknown }) {
  if (value == null || typeof value !== "object") return <p className="text-[11px] text-mute">sem dado</p>;
  const entries = Object.entries(value as Record<string, unknown>).filter(([key]) => !["source", "source_timestamp", "scope"].includes(key));
  if (!entries.length) return <p className="text-[11px] text-mute">sem dado</p>;
  return (
    <div className="grid gap-2">
      {entries.map(([key, item]) => (
        <Field key={key} name={key} value={item} />
      ))}
    </div>
  );
}

function Field({ name, value }: { name: string; value: unknown }) {
  if (value == null) return <SignedRow label={labelOf(name)} value={null} />;
  if (typeof value === "number") {
    if (Math.abs(value) > 1 || name.endsWith("_raw") || name.includes("interest") || name.includes("price_index") || name.includes("_change")) {
      return (
        <div className="flex items-center justify-between gap-3 text-[11px]">
          <span className="text-mute">{labelOf(name)}</span>
          <span className="font-mono">{value.toLocaleString("en-US", { maximumFractionDigits: 8 })}</span>
        </div>
      );
    }
    if (name.includes("quality") || name.includes("percentile") || name.includes("freshness") || name.includes("confidence") || name.includes("intensity")) {
      return <ProbRow label={labelOf(name)} value={value} />;
    }
    return <SignedRow label={labelOf(name)} value={value} />;
  }
  if (typeof value === "string" || typeof value === "boolean") {
    return (
      <div className="flex items-center justify-between gap-3 text-[11px]">
        <span className="text-mute">{labelOf(name)}</span>
        <span className="font-mono">{String(value)}</span>
      </div>
    );
  }
  if (typeof value === "object") {
    return (
      <div className="border-t border-line pt-2">
        <div className="mb-1 text-[11px] text-mute">{labelOf(name)}</div>
        <Fields value={value} />
      </div>
    );
  }
  return null;
}

function SignedRow({ label, value }: { label: string; value: number | null }) {
  const shown = value == null || Number.isNaN(value) ? null : Math.max(-1, Math.min(1, value));
  return (
    <div className="grid grid-cols-[120px_1fr_52px] items-center gap-2 text-[11px]">
      <span className="text-mute">{label}</span>
      <SignedBar value={shown} />
      <span className={`text-right font-mono ${signedClass(shown)}`}>{shown == null ? "sem dado" : shown.toFixed(2)}</span>
    </div>
  );
}

function ProbRow({ label, value, mark }: { label: string; value: number | null; mark?: number | null }) {
  const shown = value == null || Number.isNaN(value) ? null : Math.max(0, Math.min(1, value));
  return (
    <div>
      <div className="mb-1 flex items-center justify-between text-[11px]">
        <span className="text-mute">{label}</span>
        <span className="font-mono text-lg text-paper">{shown == null ? "sem dado" : pct(shown)}</span>
      </div>
      <div className="relative h-2 bg-[#1a2128]">
        <div className="absolute inset-y-0 left-0 bg-[#8fb8d6]" style={{ width: `${(shown ?? 0) * 100}%` }} />
        {mark != null ? <div className="absolute inset-y-0 w-px bg-paper" style={{ left: `${Math.max(0, Math.min(1, mark)) * 100}%` }} /> : null}
      </div>
    </div>
  );
}

function SignedBar({ value }: { value: number | null }) {
  if (value == null) return <div className="h-2 bg-[#1a2128]" />;
  const width = Math.abs(value) * 50;
  const left = value >= 0 ? 50 : 50 - width;
  const color = value > 0 ? "#3ddc97" : value < 0 ? "#ff6b6b" : "#8b98a5";
  return (
    <div className="relative h-2 bg-[#1a2128]">
      <div className="absolute inset-y-0 left-1/2 w-px bg-[#3a4650]" />
      <div className="absolute inset-y-0" style={{ left: `${left}%`, width: `${width}%`, background: color }} />
    </div>
  );
}

function flag(value: boolean | null | undefined) {
  if (value == null) return "sem dado";
  return value ? "sim" : "não";
}

function effectText(effect?: string, error?: string | null) {
  if (effect && EFFECTS[effect]) return EFFECTS[effect];
  if (error) {
    const label = error.startsWith("jev_http_") ? error.replace("jev_http_", "HTTP ") : error;
    return label.length > 28 ? `${label.slice(0, 28)}…` : label;
  }
  return "NO DATA";
}

function effectClass(effect?: string, error?: string | null) {
  if (effect === "confirm") return "text-[#3ddc97]";
  if (effect === "shadow_confirm") return "text-[#f3c969]";
  if (effect === "veto" || effect === "shadow_veto" || effect === "shadow_unavailable" || error) return "text-[#ff6b6b]";
  return "text-mute";
}

function labelOf(key: string) {
  return key.replaceAll("_", " ").toUpperCase();
}
