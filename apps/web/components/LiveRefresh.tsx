"use client";

import { useRouter } from "next/navigation";
import { useEffect } from "react";

export function LiveRefresh() {
  const router = useRouter();
  useEffect(() => {
    const source = new EventSource("/api/stream");
    let timer: number | null = null;
    const refresh = () => {
      if (timer !== null) return;
      timer = window.setTimeout(() => {
        timer = null;
        router.refresh();
      }, 1500);
    };
    for (const event of ["market_update", "decision", "trade", "position", "engine_status"]) {
      source.addEventListener(event, refresh);
    }
    return () => {
      source.close();
      if (timer !== null) window.clearTimeout(timer);
    };
  }, [router]);
  return null;
}
