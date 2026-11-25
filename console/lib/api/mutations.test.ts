import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, fireEvent, renderHook, screen, waitFor, within } from "@testing-library/react";
import { createElement, type ReactNode } from "react";
import { describe, expect, it, vi } from "vitest";

import { RequestsView } from "@/app/(shell)/requests/requests";
import { ApiClient } from "@/lib/api/client";
import { ApiProvider, useChangeConfig, useMarkHandled } from "@/lib/api/hooks";
import * as fixtures from "@/mock/fixtures";
import { fakeApi, json, ORIGIN, renderConsole } from "@/tests/render-console";

// A query client that would retry anything three times: the writes must not let it.
function retryingClient() {
  return new QueryClient({
    defaultOptions: { mutations: { retry: 3, retryDelay: 0 }, queries: { retry: 3 } },
  });
}

function wrapper(queryClient: QueryClient) {
  const client = new ApiClient({ origin: ORIGIN, onUnauthorized: vi.fn() });
  client.setCsrfToken("csrf-test");
  return function Providers({ children }: { children: ReactNode }) {
    return createElement(
      QueryClientProvider,
      { client: queryClient },
      createElement(ApiProvider, { client }, children),
    );
  };
}

describe("the writes", () => {
  it("Mark handled is sent once, never retried, when it fails", async () => {
    const { writes } = fakeApi(() => json({ detail: "Service unavailable" }, 503));
    const { result } = renderHook(() => useMarkHandled(), { wrapper: wrapper(retryingClient()) });

    await act(async () => {
      result.current.mutate(96);
    });

    await waitFor(() => expect(result.current.isError).toBe(true));
    expect(writes()).toHaveLength(1);
  });

  it("a config change is sent once, never retried, when the network fails", async () => {
    const fetchMock = vi.fn<typeof fetch>(async () => {
      throw new TypeError("network down");
    });
    vi.stubGlobal("fetch", fetchMock);
    const { result } = renderHook(() => useChangeConfig(), { wrapper: wrapper(retryingClient()) });

    await act(async () => {
      result.current.mutate({ key: "calls", value: "off" });
    });

    await waitFor(() => expect(result.current.isError).toBe(true));
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it("pressing Mark handled sends nothing until it is confirmed, and Cancel sends nothing", async () => {
    const { writes } = fakeApi((sent) =>
      sent.method === "GET"
        ? json({ requests: fixtures.QUEUE.filter((row) => row.handled === null) })
        : json(
            { source: "requests", id: 96, handled: { by: "clinic_admin", at: "2026-11-25T17:30:05+00:00" } },
            201,
          ),
    );
    renderConsole(createElement(RequestsView));
    await screen.findAllByText("Metformin 500 mg at Walgreens Oak Ave");
    const row = () => [...document.querySelectorAll<HTMLElement>('[data-request="96"]')].at(-1)!;

    fireEvent.click(within(row()).getByRole("button", { name: "Mark handled" }));
    const dialog = await screen.findByRole("alertdialog");
    expect(dialog.textContent).toContain("Mark this request as handled?");
    expect(dialog.textContent).toContain("This cannot be undone here.");
    expect(writes()).toEqual([]);

    fireEvent.click(within(dialog).getByRole("button", { name: "Cancel" }));
    await waitFor(() => expect(screen.queryByRole("alertdialog")).toBeNull());
    expect(writes()).toEqual([]);

    fireEvent.click(within(row()).getByRole("button", { name: "Mark handled" }));
    fireEvent.click(
      within(await screen.findByRole("alertdialog")).getByRole("button", { name: "Mark handled" }),
    );
    await waitFor(() => expect(writes()).toHaveLength(1));
    expect(writes()[0]).toMatchObject({ method: "POST", path: "/queue/requests/96/handled", body: {} });
  });
});
