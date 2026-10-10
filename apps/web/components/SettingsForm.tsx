"use client";

import { useRouter } from "next/navigation";
import { useState } from "react";

type Strategy = {
  key: string;
  label: string;
  enabled: boolean;
  mode: string;
  call_model: boolean;
};

export function SettingsForm({
  strategies,
  limits,
  tradingEnabled,
}: {
  strategies: Strategy[];
  limits: Record<string, number | string> | null;
  tradingEnabled: boolean;
}) {
  const router = useRouter();
  const [message, setMessage] = useState<string | null>(null);

  async function send(path: string, body: Record<string, unknown>) {
    const response = await fetch(`/api/engine/${path}`, {
      method: path.startsWith("engine/") ? "POST" : "PATCH",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(body),
    });
    const payload = await response.json().catch(() => ({}));
    setMessage(response.ok ? "registrado" : `recusado ${response.status} ${JSON.stringify(payload)}`);
    router.refresh();
  }

  return (
    <div className="grid gap-4">
      {message ? <div className="border border-line px-3 py-2 font-mono text-xs">{message}</div> : null}
      <div className="flex gap-2">
        <button
          type="button"
          className="border border-[#c6a15b] px-3 py-2 text-xs tracking-[0.14em]"
          onClick={() => {
            if (window.prompt("Digite STOP para parar novas entradas") === "STOP") {
              void send("engine/kill-switch", { confirm: "STOP" });
            }
          }}
        >
          STOP ALL TRADING
        </button>
        <button
          type="button"
          className="border border-line px-3 py-2 text-xs tracking-[0.14em] disabled:opacity-40"
          disabled={tradingEnabled}
          onClick={() => {
            if (window.prompt("Digite RESUME para reabrir entradas") === "RESUME") {
              void send("engine/resume", { confirm: "RESUME" });
            }
          }}
        >
          RESUME
        </button>
      </div>
      {strategies.map((strategy) => (
        <form
          key={strategy.key}
          className="grid gap-2 border border-line p-3 text-xs md:grid-cols-4"
          onSubmit={(event) => {
            event.preventDefault();
            const form = new FormData(event.currentTarget);
            const mode = String(form.get("mode"));
            const confirm = window.prompt(`Digite ${mode.toUpperCase()} para confirmar`) ?? "";
            void send(`strategies/${strategy.key}`, {
              enabled: form.get("enabled") === "on",
              mode,
              call_model: form.get("call_model") === "on",
              confirm,
            });
          }}
        >
          <div className="md:col-span-4 text-[11px] tracking-[0.14em] text-mute">{strategy.label}</div>
          <label className="flex items-center gap-2">
            <input name="enabled" type="checkbox" defaultChecked={strategy.enabled} /> enabled
          </label>
          <select name="mode" defaultValue={strategy.mode} className="border border-line bg-ink px-2 py-1">
            <option value="disabled">disabled</option>
            <option value="shadow">shadow</option>
            <option value="paper">paper</option>
            <option value="live">live</option>
          </select>
          <label className="flex items-center gap-2">
            <input name="call_model" type="checkbox" defaultChecked={strategy.call_model} /> model calls
          </label>
          <button type="submit" className="border border-line px-2 py-1">
            salvar
          </button>
        </form>
      ))}
      {limits ? (
        <form
          className="grid gap-2 border border-line p-3 text-xs md:grid-cols-2"
          onSubmit={(event) => {
            event.preventDefault();
            const form = new FormData(event.currentTarget);
            if (window.prompt("Digite CONFIRM para alterar risco") !== "CONFIRM") return;
            const body: Record<string, unknown> = { confirm: "CONFIRM" };
            for (const key of ["risk_per_trade", "max_daily_drawdown", "max_open_positions"]) {
              body[key] = key === "max_open_positions" ? Number(form.get(key)) : Number(form.get(key));
            }
            void send("risk/limits", body);
          }}
        >
          <div className="md:col-span-2 text-[11px] tracking-[0.14em] text-mute">RISK LIMITS</div>
          {["risk_per_trade", "max_daily_drawdown", "max_open_positions"].map((key) => (
            <label key={key} className="grid gap-1">
              <span className="text-mute">{key}</span>
              <input name={key} defaultValue={String(limits[key] ?? "")} className="border border-line bg-ink px-2 py-1 font-mono" />
            </label>
          ))}
          <button type="submit" className="border border-line px-2 py-1 md:col-span-2">
            salvar limites
          </button>
        </form>
      ) : null}
    </div>
  );
}
