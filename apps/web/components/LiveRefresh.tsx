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
    const poll = window.setInterval(refresh, 5000);
    return () => {
      source.close();
      window.clearInterval(poll);
      if (timer !== null) window.clearTimeout(timer);
    };
  }, [router]);
  return null;
}
