import Link from "next/link";
import { Empty, Panel } from "@/components/Shell";
import { pct, shortTime, signedClass } from "@/lib/utils";

export type JevReview = {
  decision_id: string;
  symbol?: string;
  timestamp?: string;
  action?: string;
  confidence?: number;
  effect?: string;
  required_continuation?: number | null;
  reason_codes?: string[];
  state?: {
    symbol?: string;
    baseline_action?: string;
    baseline_confidence?: number;
    baseline_scores?: Record<string, number | null>;
    taker_flow_1m?: number | null;
    return_60m?: number | null;
    range_60m?: number | null;
    breakout?: boolean | null;
    breakdown?: boolean | null;
    intelligence?: Record<string, unknown> | null;
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

const EFFECTS: Record<string, string> = {
  confirm: "CONFIRMOU",
  veto: "VETOU",
  assessed: "SÓ LEU",
  fallback: "BASELINE",
};

const SCORES: Array<[string, string]> = [
  ["trend_score", "TENDÊNCIA"],
  ["momentum_score", "MOMENTO"],
  ["volume_score", "VOLUME"],
  ["orderflow_score", "FLUXO"],
  ["structure_score", "ESTRUTURA"],
  ["volatility_score", "VOLATILIDADE"],
  ["liquidity_score", "LIQUIDEZ"],
];

export function JevDesk({ rows, selected }: { rows: JevReview[] | null; selected: JevReview | null }) {
  if (!rows) return <Panel title="JEV"><Empty label="ENGINE OFFLINE" /></Panel>;
  if (!rows.length) return <Panel title="JEV"><Empty label="AINDA SEM LEITURA" /></Panel>;
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
                    <span className="text-mute">{row.state?.baseline_action ?? "—"}</span>
                    <span className={effectClass(row.effect, row.response?.error)}>{effectText(row.effect, row.response?.error)}</span>
                  </div>
                </Link>
              </li>
            );
          })}
        </ul>
      </Panel>
      {selected ? <Reading review={selected} /> : <Empty />}
    </div>
  );
}

function Reading({ review }: { review: JevReview }) {
  const state = review.state ?? {};
  const answer = review.response ?? {};
  const required = review.required_continuation;
  return (
    <div className="grid gap-4">
      <Panel
        title={`${review.symbol ?? "NO DATA"} · ${shortTime(review.timestamp)}`}
        aside={<span className={`font-mono text-[11px] ${effectClass(review.effect, answer.error)}`}>{effectText(review.effect, answer.error)}</span>}
      >
        <div className="flex flex-wrap items-end justify-between gap-3">
          <div>
            <div className="text-[11px] text-mute">BASELINE PEDIU</div>
            <div className="font-mono text-2xl">{state.baseline_action ?? "NO DATA"}</div>
            <div className="font-mono text-[11px] text-mute">confiança {pct(state.baseline_confidence ?? null)}</div>
          </div>
          <div className="text-right">
            <div className="text-[11px] text-mute">JEV DEVOLVEU</div>
            <div className="font-mono text-2xl">{review.action ?? "NO DATA"}</div>
            <div className="font-mono text-[11px] text-mute">
              {answer.is_mock ? "MOCK" : answer.model ?? "NO DATA"}
              {answer.latency_ms != null ? ` · ${Math.round(answer.latency_ms)} ms` : ""}
            </div>
          </div>
        </div>
      </Panel>
      <div className="grid gap-4 lg:grid-cols-2">
        <Panel title="O QUE ENTROU">
          <div className="grid gap-3">
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
          </div>
        </Panel>
        <Panel title="O QUE VOLTOU">
          <div className="grid gap-4">
            <ProbRow label="CONTINUAÇÃO" value={answer.trend_continuation_probability ?? null} mark={required ?? null} />
            <ProbRow label="REVERSÃO" value={answer.reversal_probability ?? null} />
            <ProbRow label="FALSO ROMPIMENTO" value={answer.false_breakout_probability ?? null} />
            <p className="text-[11px] text-mute">
              {required == null
                ? "O corte de continuação não foi gravado nesta leitura."
                : `Confirmar exige continuação de pelo menos ${pct(required)}. Abaixo disso o Jev veta.`}
            </p>
            {answer.error ? <p className="font-mono text-[11px] text-[#ff6b6b]">{answer.error}</p> : null}
          </div>
        </Panel>
      </div>
      <IntelligenceBlock intelligence={state.intelligence ?? null} />
    </div>
  );
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
    if (Math.abs(value) > 1) {
      return (
        <div className="flex items-center justify-between gap-3 text-[11px]">
          <span className="text-mute">{labelOf(name)}</span>
          <span className="font-mono">{value.toLocaleString("en-US", { maximumFractionDigits: 4 })}</span>
        </div>
      );
    }
    if (name.includes("quality") || name.includes("percentile") || name.includes("freshness") || name.includes("confidence")) {
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
  if (effect === "veto" || error) return "text-[#ff6b6b]";
  return "text-mute";
}

function labelOf(key: string) {
  return key.replaceAll("_", " ").toUpperCase();
}
