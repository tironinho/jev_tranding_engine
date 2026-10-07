import { Empty, Panel } from "@/components/Shell";
import { engineFetch } from "@/lib/engine";

export const dynamic = "force-dynamic";

type Detail = {
  public_id: string;
  problem_statement: string;
  hypothesis: string;
  status: string;
  parent_version: string;
  challenger_version: string | null;
  rejection_reason: string | null;
  decision: string | null;
  git_branch: string | null;
  dataset_version: string | null;
  code_locked: boolean;
  timeline: Array<{ at: string; event: string; reason?: string }>;
  result: Record<string, unknown>;
  prompt: string;
};

export default async function ExperimentDetailPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  const result = await engineFetch<Detail>(`/api/evolution/experiments/${id}`);
  if (!result.ok || !result.data) {
    return <Panel title="EXPERIMENT"><Empty /></Panel>;
  }
  const experiment = result.data;
  return (
    <div className="grid gap-4">
      <Panel title={experiment.public_id}>
        <dl className="grid gap-2 text-[12px] md:grid-cols-2">
          <Field label="PROBLEM" value={experiment.problem_statement} />
          <Field label="HYPOTHESIS" value={experiment.hypothesis} />
          <Field label="STAGE" value={experiment.status} />
          <Field label="CHAMPION" value={experiment.parent_version} />
          <Field label="CHALLENGER" value={experiment.challenger_version ?? "NO DATA"} />
          <Field label="DECISION" value={experiment.rejection_reason ?? experiment.decision ?? "NO DATA"} />
          <Field label="BRANCH" value={experiment.git_branch ?? "NO DATA"} />
          <Field label="DATASET" value={experiment.dataset_version ?? "NO DATA"} />
          <Field label="CODE LOCK" value={experiment.code_locked ? "LOCKED" : "OPEN"} />
        </dl>
      </Panel>
      <Panel title="TIMELINE">
        {!experiment.timeline.length ? <Empty /> : (
          <ol className="space-y-1 font-mono text-[11px]">
            {experiment.timeline.map((item, index) => (
              <li key={`${item.at}-${index}`}>{item.at} {item.event} {item.reason ?? ""}</li>
            ))}
          </ol>
        )}
      </Panel>
      <Panel title="VALIDATION">
        <pre className="overflow-x-auto font-mono text-[11px]">{Object.keys(experiment.result).length ? JSON.stringify(experiment.result, null, 2) : "NO DATA"}</pre>
      </Panel>
      <Panel title="AGENT BRIEF">
        <pre className="overflow-x-auto whitespace-pre-wrap font-mono text-[11px]">{experiment.prompt}</pre>
      </Panel>
    </div>
  );
}

function Field({ label, value }: { label: string; value: string }) {
  return (
    <div className="border-b border-line/50 py-1">
      <dt className="text-mute">{label}</dt>
      <dd>{value}</dd>
    </div>
  );
}
