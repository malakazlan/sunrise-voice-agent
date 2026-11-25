"use client";

import { createContext, useContext } from "react";

import type { Session } from "@/lib/api/schemas";

// Who is signed in, whether the office is open and the clinic's time zone, from the session.
export const ClinicContext = createContext<Session | null>(null);

export function useClinic(): Session {
  const session = useContext(ClinicContext);
  if (session === null) {
    throw new Error("useClinic needs the signed-in shell");
  }
  return session;
}
