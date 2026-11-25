"use client";

import { useEffect, useState } from "react";

// The time now, updated every `everyMs`: for running times that tick on screen.
export function useNow(everyMs: number): Date {
  const [now, setNow] = useState(() => new Date());
  useEffect(() => {
    const timer = setInterval(() => setNow(new Date()), everyMs);
    return () => clearInterval(timer);
  }, [everyMs]);
  return now;
}
