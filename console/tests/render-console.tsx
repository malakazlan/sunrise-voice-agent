import { QueryClient, QueryClientProvider, type QueryClientConfig } from "@tanstack/react-query";
import { render } from "@testing-library/react";
import type { ReactElement } from "react";
import { vi } from "vitest";

import { ClinicContext } from "@/components/clinic";
import { ApiClient } from "@/lib/api/client";
import { ApiProvider } from "@/lib/api/hooks";
import * as fixtures from "@/mock/fixtures";

export const ORIGIN = "https://admin.clinic.test";

export interface Sent {
  method: string;
  path: string;
  body: unknown;
  headers: Headers;
}

// A stand-in for the admin API inside a test: each request is recorded and answered by `answer`.
export function fakeApi(answer: (sent: Sent) => Promise<Response> | Response) {
  const sent: Sent[] = [];
  const fetchMock = vi.fn<typeof fetch>(async (input, init) => {
    const url = new URL(String(input));
    const request: Sent = {
      method: init?.method ?? "GET",
      path: `${url.pathname.replace("/admin/api", "")}${url.search}`,
      body: init?.body === undefined ? undefined : JSON.parse(String(init.body)),
      headers: new Headers(init?.headers),
    };
    sent.push(request);
    return answer(request);
  });
  vi.stubGlobal("fetch", fetchMock);
  return { sent, writes: () => sent.filter((each) => each.method !== "GET") };
}

export function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
}

// The console as a signed-in page sees it: the session, the one fetch wrapper with its token, and a
// query client with the given defaults (the app's own unless a test says otherwise).
export function renderConsole(ui: ReactElement, config: QueryClientConfig = {}) {
  const queryClient = new QueryClient(config);
  const client = new ApiClient({ origin: ORIGIN, onUnauthorized: vi.fn() });
  client.setCsrfToken("csrf-test");
  const view = render(
    <QueryClientProvider client={queryClient}>
      <ApiProvider client={client}>
        <ClinicContext.Provider value={fixtures.session()}>{ui}</ClinicContext.Provider>
      </ApiProvider>
    </QueryClientProvider>,
  );
  return { ...view, queryClient };
}

// Everything the query library holds, queries and mutations, as text.
export function everythingCached(queryClient: QueryClient): string {
  return JSON.stringify({
    queries: queryClient
      .getQueryCache()
      .getAll()
      .map((query) => query.state),
    mutations: queryClient
      .getMutationCache()
      .getAll()
      .map((mutation) => mutation.state),
  });
}
