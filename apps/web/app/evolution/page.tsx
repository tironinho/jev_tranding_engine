import Link from "next/link";
import { Empty, Panel } from "@/components/Shell";
import { engineFetch } from "@/lib/engine";
import { num, pct } from "@/lib/utils";

export const dynamic = "force-dynamic";

type Status = {
  status: string;
  controls: Record<string, boolean>;
  coding_agent: string;
  champions: number;
  anomalies: number;
  experiments_running: number;
  experiments_rejected: number;
  last_decision: string;
  min_sample_size: number;
  live_promotion: string;
};

type Champion = {
  family: string;
  version: string;
  age_days: number;
  trades: number;
  expectancy_r: number | null;
  profit_factor: number | null;
  max_drawdown: number | null;
  net_pnl: number | null;
  health: number | null;
};

type ExperimentRow = {
  public_id: string;
  strategy_family: string;
  hypothesis: string;
  status: string;
  parent_version: string;
  challenger_version: string | null;
  decision: string | null;
  created_at: string;
  rejection_reason: string | null;
};

type Node = { version: string; family: string; parent: string | null; status: string };

export default async function EvolutionPage() {
  const [status, champions, experiments, anomalies, tree] = await Promise.all([
    engineFetch<Status>("/api/evolution/status"),
    engineFetch<{ rows: Champion[] }>("/api/evolution/champions"),
    engineFetch<{ rows: ExperimentRow[] }>("/api/evolution/experiments"),
    engineFetch<{ rows: Array<{ code: string; symbol: string | null; sample_size: number; anomaly_confidence: number }> }>("/api/evolution/anomalies"),
    engineFetch<{ nodes: Node[] }>("/api/evolution/tree"),
  ]);
  if (!status.ok || !status.data) {
    return <Panel title="EVOLUTION"><div className="font-mono text-xs text-mute">ENGINE OFFLINE — NO DATA</div></Panel>;
  }
  const info = status.data;
  return (
    <div className="grid gap-4">
      <section className="border border-line bg-panel px-3 py-3 text-[12px]">
        <div className="text-[11px] tracking-[0.16em] text-mute">EVOLUTION ENGINE</div>
        <div className="mt-2 font-mono">{info.status.toUpperCase()}</div>
        <dl className="mt-3 grid grid-cols-2 gap-2 md:grid-cols-4">
          <Metric label="CHAMPIONS" value={String(info.champions)} />
          <Metric label="ANOMALIES" value={String(info.anomalies)} />
          <Metric label="RUNNING" value={String(info.experiments_running)} />
          <Metric label="REJECTED" value={String(info.experiments_rejected)} />
          <Metric label="LAST DECISION" value={info.last_decision} />
          <Metric label="MIN SAMPLE" value={String(info.min_sample_size)} />
          <Metric label="CODING AGENT" value={info.coding_agent.toUpperCase()} />
          <Metric label="LIVE PROMOTION" value={info.live_promotion.toUpperCase()} />
        </dl>
        <div className="mt-3 flex flex-wrap gap-2 text-[10px] tracking-[0.12em] text-mute">
          <span>RESEARCH {info.controls.auto_research ? "ON" : "OFF"}</span>
          <span>BUILD {info.controls.auto_build ? "ON" : "OFF"}</span>
          <span>AUTO SHADOW {info.controls.auto_shadow_promotion ? "ON" : "OFF"}</span>
          <span>AUTO PAPER {info.controls.auto_paper_promotion ? "ON" : "OFF"}</span>
        </div>
      </section>
      <Panel title="CHAMPIONS">
        <ChampionTable rows={champions.data?.rows ?? null} />
      </Panel>
      <Panel title="EXPERIMENTS">
        <ExperimentTable rows={experiments.ok ? experiments.data?.rows ?? [] : null} />
      </Panel>
      <div className="grid gap-4 lg:grid-cols-2">
        <Panel title="ANOMALIES">
          {!anomalies.data?.rows.length ? <Empty /> : (
            <ul className="space-y-2 font-mono text-[11px]">
              {anomalies.data.rows.map((row) => (
                <li key={`${row.code}-${row.symbol}`}>{row.code} {row.symbol ?? "ALL"} n={row.sample_size} conf={row.anomaly_confidence}</li>
              ))}
            </ul>
          )}
        </Panel>
        <Panel title="FAMILY TREE">
          <FamilyTree nodes={tree.data?.nodes ?? []} />
        </Panel>
      </div>
    </div>
  );
}

function Metric({ label, value }: { label: string; value: string }) {
  return (
    <div>
      <dt className="text-mute">{label}</dt>
      <dd className="font-mono">{value}</dd>
    </div>
  );
}

function ChampionTable({ rows }: { rows: Champion[] | null }) {
  if (!rows?.length) return <Empty />;
  return (
    <div className="overflow-x-auto">
      <table className="w-full text-left text-[11px]">
        <thead className="text-mute">
          <tr className="border-b border-line">
            {["FAMILY", "VERSION", "AGE", "TRADES", "EXPECTANCY", "PF", "MAX DD", "NET", "HEALTH"].map((head) => (
              <th key={head} className="px-2 py-2 font-normal tracking-[0.08em]">{head}</th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr key={row.version} className="border-b border-line/70 font-mono">
              <td className="px-2 py-2">{row.family}</td>
              <td className="px-2 py-2">{row.version}</td>
              <td className="px-2 py-2">{row.age_days}d</td>
              <td className="px-2 py-2">{row.trades}</td>
              <td className="px-2 py-2">{row.expectancy_r == null ? "NO DATA" : `${num(row.expectancy_r, 2)}R`}</td>
              <td className="px-2 py-2">{row.profit_factor == null ? "NO DATA" : num(row.profit_factor, 2)}</td>
              <td className="px-2 py-2">{row.max_drawdown == null ? "NO DATA" : pct(row.max_drawdown)}</td>
              <td className="px-2 py-2">{row.net_pnl == null ? "NO DATA" : num(row.net_pnl, 2)}</td>
              <td className="px-2 py-2">{row.health == null ? "NO DATA" : num(row.health, 0)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function ExperimentTable({ rows }: { rows: ExperimentRow[] | null }) {
  if (!rows) return <Empty />;
  if (!rows.length) return <Empty label="NO DATA" />;
  return (
    <div className="overflow-x-auto">
      <table className="w-full text-left text-[11px]">
        <thead className="text-mute">
          <tr className="border-b border-line">
            {["EXP", "STRATEGY", "HYPOTHESIS", "STAGE", "CHAMPION", "CHALLENGER", "RESULT"].map((head) => (
              <th key={head} className="px-2 py-2 font-normal tracking-[0.08em]">{head}</th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr key={row.public_id} className="border-b border-line/70">
              <td className="px-2 py-2 font-mono"><Link href={`/evolution/${row.public_id}`}>{row.public_id}</Link></td>
              <td className="px-2 py-2">{row.strategy_family}</td>
              <td className="px-2 py-2 max-w-xs truncate">{row.hypothesis}</td>
              <td className="px-2 py-2 font-mono">{row.status}</td>
              <td className="px-2 py-2 font-mono">{row.parent_version}</td>
              <td className="px-2 py-2 font-mono">{row.challenger_version ?? "—"}</td>
              <td className="px-2 py-2 font-mono">{row.rejection_reason ?? row.decision ?? "—"}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function FamilyTree({ nodes }: { nodes: Node[] }) {
  if (!nodes.length) return <Empty />;
  return (
    <ul className="space-y-1 font-mono text-[12px]">
      {nodes.map((node) => (
        <li key={node.version}>
          {node.parent ? `${node.parent} → ` : ""}
          {node.version} <span className="text-mute">{node.status}</span>
        </li>
      ))}
    </ul>
  );
}
