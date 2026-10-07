import { cn } from "@/lib/utils";

export function Badge({
  children,
  tone = "neutral",
  className,
}: {
  children: React.ReactNode;
  tone?: "neutral" | "paper" | "shadow" | "live" | "blocked" | "off" | "error";
  className?: string;
}) {
  const tones = {
    neutral: "border-line text-mute",
    paper: "border-[#3d4c59] text-[#c5d0da]",
    shadow: "border-[#3a4450] text-[#9aa8b5]",
    live: "border-[#c6a15b] text-[#e4d2a4] bg-[#2a2418]",
    blocked: "border-[#c6a15b] text-[#e4d2a4]",
    off: "border-[#2a3138] text-[#6d7883]",
    error: "border-[#6e4c4c] text-[#d0b0b0]",
  };
  return (
    <span className={cn("inline-flex items-center border px-1.5 py-0.5 text-[10px] tracking-[0.14em]", tones[tone], className)}>
      {children}
    </span>
  );
}

export function modeTone(mode: string, liveArmed = false): "paper" | "shadow" | "live" | "blocked" | "off" | "error" {
  if (mode === "live") return liveArmed ? "live" : "blocked";
  if (mode === "paper") return "paper";
  if (mode === "shadow") return "shadow";
  if (mode === "error") return "error";
  return "off";
}

export function modeLabel(mode: string, liveArmed = false): string {
  if (mode === "live" && !liveArmed) return "LIVE BLOCKED";
  if (mode === "disabled" || mode === "off") return "OFF";
  return mode.toUpperCase();
}
