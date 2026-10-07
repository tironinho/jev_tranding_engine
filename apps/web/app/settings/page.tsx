import { RiskPanel } from "@/components/Inspector";
import { Panel } from "@/components/Shell";
import { SettingsForm } from "@/components/SettingsForm";
import { engineFetch } from "@/lib/engine";

export const dynamic = "force-dynamic";

export default async function SettingsPage() {
  const [strategies, limits, status] = await Promise.all([
    engineFetch<{ strategies: Array<{ key: string; label: string; enabled: boolean; mode: string; call_model: boolean }> }>("/api/strategies"),
    engineFetch<Record<string, number | string>>("/api/risk/limits"),
    engineFetch<{ trading_enabled: boolean }>("/api/engine/status"),
  ]);
  if (!strategies.ok || !strategies.data) {
    return <Panel title="SETTINGS"><div className="font-mono text-xs text-mute">ENGINE OFFLINE — NO DATA</div></Panel>;
  }
  return (
    <div className="grid gap-4">
      <Panel title="OPERAÇÃO">
        <SettingsForm
          strategies={strategies.data.strategies}
          limits={limits.data}
          tradingEnabled={Boolean(status.data?.trading_enabled)}
        />
      </Panel>
      <Panel title="LIMITES EFETIVOS">
        <RiskPanel limits={limits.data} />
      </Panel>
    </div>
  );
}
