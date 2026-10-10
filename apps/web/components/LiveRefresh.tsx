"use client";

import { usePathname, useRouter } from "next/navigation";
import { useEffect } from "react";

export function LiveRefresh() {
  const router = useRouter();
  const pathname = usePathname();
  useEffect(() => {
    const source = new EventSource("/api/stream");
    let timer: number | null = null;
    const refresh = () => {
      if (timer !== null) return;
      timer = window.setTimeout(() => {
        timer = null;
        router.refresh();
      }, pathname === "/jev" ? 10000 : 1500);
    };
    const events = pathname === "/jev"
      ? ["decision", "trade", "position", "engine_status"]
      : ["market_update", "decision", "trade", "position", "engine_status"];
    for (const event of events) {
      source.addEventListener(event, refresh);
    }
    const poll = window.setInterval(refresh, pathname === "/jev" ? 15000 : 5000);
    return () => {
      source.close();
      window.clearInterval(poll);
      if (timer !== null) window.clearTimeout(timer);
    };
  }, [pathname, router]);
  return null;
}
