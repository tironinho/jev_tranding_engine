"use client";

import { useMemo, useState } from "react";
import { CartesianGrid, Line, LineChart, ResponsiveContainer, Tooltip, XAxis, YAxis, BarChart, Bar } from "recharts";
import { Empty } from "@/components/Shell";

const COLORS: Record<string, string> = {
  baseline: "#9fb0c0",
  baseline_jev: "#c6b48a",
  baseline_openai_jev: "#8aa89a",
};

type Point = { t: string; equity: number; indexed?: number | null; drawdown?: number; mark?: boolean };

export function EquityCurve({ series }: { series: Record<string, Point[]> | null }) {
  const [normalize, setNormalize] = useState(true);
  const data = useMemo(() => merge(series, normalize ? "indexed" : "equity"), [series, normalize]);
  if (!series) return <Empty />;
  const onlyInitial = Object.values(series).every((points) => points.filter((point) => point.mark !== undefined ? !point.mark : true).length <= 1);
  return (
    <div>
      <div className="mb-2 flex items-center justify-between text-[11px] text-mute">
        <span>{onlyInitial ? "sem trades fechados — curva no capital inicial" : "equity"}</span>
        <button type="button" className="border border-line px-2 py-1" onClick={() => setNormalize((value) => !value)}>
          {normalize ? "BASE 100" : "CAPITAL"}
        </button>
      </div>
      <div className="h-64">
        <ResponsiveContainer width="100%" height="100%">
          <LineChart data={data}>
            <CartesianGrid stroke="#24303a" vertical={false} />
            <XAxis dataKey="t" hide />
            <YAxis stroke="#8b98a5" fontSize={11} width={48} />
            <Tooltip contentStyle={{ background: "#13181d", border: "1px solid #24303a", fontSize: 12 }} />
            {Object.keys(COLORS).map((key) => (
              <Line key={key} type="monotone" dataKey={key} stroke={COLORS[key]} dot={false} strokeWidth={1.4} connectNulls />
            ))}
          </LineChart>
        </ResponsiveContainer>
      </div>
    </div>
  );
}

export function DrawdownChart({ series }: { series: Record<string, Point[]> | null }) {
  const data = useMemo(() => merge(series, "drawdown"), [series]);
  if (!data.length) return <Empty />;
  return (
    <div className="h-48">
      <ResponsiveContainer width="100%" height="100%">
        <LineChart data={data}>
          <CartesianGrid stroke="#24303a" vertical={false} />
          <XAxis dataKey="t" hide />
          <YAxis stroke="#8b98a5" fontSize={11} width={48} />
          <Tooltip contentStyle={{ background: "#13181d", border: "1px solid #24303a", fontSize: 12 }} />
          {Object.keys(COLORS).map((key) => (
            <Line key={key} type="monotone" dataKey={key} stroke={COLORS[key]} dot={false} strokeWidth={1.2} connectNulls />
          ))}
        </LineChart>
      </ResponsiveContainer>
    </div>
  );
}

export function PnLChart({
  trades,
}: {
  trades: Array<{ closed_at: string; strategy: string; net_pnl: number }> | null;
}) {
  if (!trades?.length) return <Empty />;
  const data = trades
    .slice()
    .reverse()
    .map((trade, index) => ({
      name: `${index + 1}`,
      net: trade.net_pnl,
    }));
  return (
    <div className="h-48">
      <ResponsiveContainer width="100%" height="100%">
        <BarChart data={data}>
          <CartesianGrid stroke="#24303a" vertical={false} />
          <XAxis dataKey="name" hide />
          <YAxis stroke="#8b98a5" fontSize={11} width={48} />
          <Tooltip contentStyle={{ background: "#13181d", border: "1px solid #24303a", fontSize: 12 }} />
          <Bar dataKey="net" fill="#8ea4b8" />
        </BarChart>
      </ResponsiveContainer>
    </div>
  );
}

function merge(series: Record<string, Point[]> | null, field: "equity" | "indexed" | "drawdown") {
  if (!series) return [];
  const map = new Map<string, Record<string, string | number | null>>();
  for (const [key, points] of Object.entries(series)) {
    for (const point of points) {
      const row = map.get(point.t) ?? { t: point.t.slice(11, 19) };
      const value = field === "equity" ? point.equity : field === "indexed" ? point.indexed ?? null : point.drawdown ?? null;
      row[key] = value;
      map.set(point.t, row);
    }
  }
  return [...map.entries()]
    .sort((a, b) => a[0].localeCompare(b[0]))
    .map((entry) => entry[1]);
}
