export type Action = "LONG" | "SHORT" | "NO_TRADE";
export type OperatingMode = "disabled" | "shadow" | "paper" | "live";

export type StrategyDecision = {
  decision_id: string;
  correlation_id: string;
  opportunity_id: string;
  snapshot_id: string;
  strategy: string;
  symbol: string;
  timestamp: string;
  action: Action;
  confidence: number;
  reason_codes: string[];
  signal_status: "valid" | "expired" | "skipped";
  mode: OperatingMode;
};
