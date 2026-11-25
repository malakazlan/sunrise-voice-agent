"use client";

import {
  useInfiniteQuery,
  useMutation,
  useQuery,
  useQueryClient,
  type InfiniteData,
} from "@tanstack/react-query";
import { createContext, createElement, useContext, useEffect, useState, type ReactNode } from "react";

import { ApiClient } from "@/lib/api/client";
import {
  appointmentsSchema,
  auditEventsSchema,
  callDetailSchema,
  callsSchema,
  guardrailsSchema,
  isCallId,
  metricsSchema,
  queueSchema,
  sessionSchema,
  slotsSchema,
  turnsSchema,
  verificationSchema,
  type EndReason,
  type LiveCall,
  type RequestType,
} from "@/lib/api/schemas";
import { openLiveStream, type LiveStreamState } from "@/lib/api/stream";

// Every read of the console, as a TanStack Query hook over the one fetch wrapper. The cache is in
// memory only. Nothing here polls: the live page uses the stream, the rest read when opened.

export const PAGE_SIZE = 50;

const ApiContext = createContext<ApiClient | null>(null);

export function ApiProvider({ client, children }: { client: ApiClient; children?: ReactNode }) {
  return createElement(ApiContext.Provider, { value: client }, children);
}

export function useApi(): ApiClient {
  const client = useContext(ApiContext);
  if (client === null) {
    throw new Error("useApi needs an ApiProvider");
  }
  return client;
}

export function useSession() {
  const api = useApi();
  return useQuery({
    queryKey: ["session"],
    queryFn: async ({ signal }) => {
      const session = await api.get("/session", sessionSchema, undefined, { signal });
      api.setCsrfToken(session.csrf_token);
      return session;
    },
    staleTime: 60_000,
  });
}

export function useSignIn() {
  const api = useApi();
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ user, password }: { user: string; password: string }) => api.signIn(user, password),
    onSuccess: (session) => {
      queryClient.removeQueries();
      queryClient.setQueryData(["session"], session);
    },
  });
}

export function useSignOut() {
  const api = useApi();
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: () => api.signOut(),
    onSettled: () => queryClient.clear(),
  });
}

function nextBefore<T>(page: T[], key: (row: T) => number): number | undefined {
  return page.length < PAGE_SIZE ? undefined : key(page[page.length - 1]);
}

// The ended calls, newest first, of those that ended for `endReason` if one is given.
export function useCalls(endReason: EndReason | undefined) {
  const api = useApi();
  return useInfiniteQuery({
    queryKey: ["calls", endReason ?? "all"],
    queryFn: async ({ pageParam, signal }) =>
      (
        await api.get(
          "/calls",
          callsSchema,
          { limit: PAGE_SIZE, before: pageParam, end_reason: endReason },
          { signal },
        )
      ).calls,
    initialPageParam: undefined as number | undefined,
    getNextPageParam: (page) => nextBefore(page, (call) => call.id),
  });
}

export function useCallDetail(callId: string) {
  const api = useApi();
  return useQuery({
    queryKey: ["call", callId],
    queryFn: async ({ signal }) =>
      api.get(`/calls/${callId}/detail`, callDetailSchema, undefined, { signal }),
    enabled: isCallId(callId),
  });
}

export function useCallTurns(callId: string) {
  const api = useApi();
  return useInfiniteQuery({
    queryKey: ["turns", callId],
    queryFn: async ({ pageParam, signal }) =>
      (
        await api.get(
          `/calls/${callId}/turns`,
          turnsSchema,
          { limit: PAGE_SIZE, after: pageParam },
          { signal },
        )
      ).turns,
    initialPageParam: undefined as number | undefined,
    getNextPageParam: (page) => (page.length < PAGE_SIZE ? undefined : page[page.length - 1].turn),
    enabled: isCallId(callId),
  });
}

export function useQueue(type: RequestType | undefined) {
  const api = useApi();
  return useInfiniteQuery({
    queryKey: ["queue", type ?? "all"],
    queryFn: async ({ pageParam, signal }) =>
      // The queue's pages are the API's own size (PAGE_SIZE); it takes no limit.
      (await api.get("/queue", queueSchema, { before: pageParam, type }, { signal })).requests,
    initialPageParam: undefined as number | undefined,
    getNextPageParam: (page) => nextBefore(page, (request) => request.id),
  });
}

export function useAppointments() {
  const api = useApi();
  return useInfiniteQuery({
    queryKey: ["appointments"],
    queryFn: async ({ pageParam, signal }) =>
      (
        await api.get(
          "/appointments",
          appointmentsSchema,
          { limit: PAGE_SIZE, before: pageParam },
          { signal },
        )
      ).appointments,
    initialPageParam: undefined as number | undefined,
    getNextPageParam: (page) => nextBefore(page, (appointment) => appointment.id),
  });
}

export const SLOT_DAYS = 7;

export function useSlots(from: string) {
  const api = useApi();
  return useQuery({
    queryKey: ["slots", from],
    queryFn: async ({ signal }) => api.get("/slots", slotsSchema, { from, days: SLOT_DAYS }, { signal }),
    placeholderData: (previous) => previous,
  });
}

export interface AuditFilter {
  callId?: string;
  action?: string;
}

export function useAudit({ callId, action }: AuditFilter) {
  const api = useApi();
  return useInfiniteQuery({
    queryKey: ["audit", callId ?? "", action ?? ""],
    queryFn: async ({ pageParam, signal }) =>
      (
        await api.get(
          "/audit",
          auditEventsSchema,
          { limit: PAGE_SIZE, before: pageParam, call_id: callId, action },
          { signal },
        )
      ).events,
    initialPageParam: undefined as number | undefined,
    getNextPageParam: (page) => nextBefore(page, (event) => event.seq),
  });
}

// The check reads the whole chain and can be slow on a long log.
export const CHAIN_CHECK_TIMEOUT_MS = 120_000;

export function useVerifyChain() {
  const api = useApi();
  return useMutation({
    mutationFn: () =>
      api.get("/audit/verify", verificationSchema, undefined, { timeoutMs: CHAIN_CHECK_TIMEOUT_MS }),
  });
}

export function useGuardrails(days: number) {
  const api = useApi();
  return useQuery({
    queryKey: ["guardrails", days],
    queryFn: async ({ signal }) => api.get("/guardrails/summary", guardrailsSchema, { days }, { signal }),
    placeholderData: (previous) => previous,
  });
}

export function useMetrics(days: number) {
  const api = useApi();
  return useQuery({
    queryKey: ["metrics", days],
    queryFn: async ({ signal }) => api.get("/metrics/summary", metricsSchema, { days }, { signal }),
    placeholderData: (previous) => previous,
  });
}

export function allRows<T>(data: InfiniteData<T[]> | undefined): T[] {
  return data?.pages.flat() ?? [];
}

export interface LiveCalls {
  calls: LiveCall[] | null;
  state: LiveStreamState;
  updatedAt: Date | null;
  // After another tab took the stream over: open it here again.
  useHere: () => void;
}

// The live calls stream while the page is shown and the tab is visible; it is closed when the tab
// is hidden and on unmount, and opened again (one) when the tab is shown. Once another tab has
// taken it over it stays closed until "Use here". The last list stays on screen, with its time,
// while the stream is paused or retrying.
export function useLiveCalls(onSignedOut: () => void): LiveCalls {
  const api = useApi();
  const queryClient = useQueryClient();
  const [live, setLive] = useState<Omit<LiveCalls, "useHere">>({
    calls: null,
    state: "connecting",
    updatedAt: null,
  });
  const [attempt, setAttempt] = useState(0);

  useEffect(() => {
    let close: (() => void) | null = null;
    let replaced = false;
    const start = () => {
      close?.();
      close = openLiveStream(api.url("/calls/live/stream"), {
        onCalls: (calls) => setLive((current) => ({ ...current, calls, updatedAt: new Date() })),
        onState: (state) => {
          replaced = state === "replaced";
          setLive((current) => ({ ...current, state }));
        },
        onSignedOut,
        onConnectionLost: () => {
          void queryClient.invalidateQueries({ queryKey: ["session"] });
        },
      });
    };
    const onVisibility = () => {
      if (document.visibilityState === "hidden") {
        close?.();
        close = null;
      } else if (close === null && !replaced) {
        start();
      }
    };
    if (document.visibilityState !== "hidden") {
      start();
    }
    document.addEventListener("visibilitychange", onVisibility);
    return () => {
      document.removeEventListener("visibilitychange", onVisibility);
      close?.();
    };
  }, [api, queryClient, onSignedOut, attempt]);

  return { ...live, useHere: () => setAttempt((count) => count + 1) };
}
