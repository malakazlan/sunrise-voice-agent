import { act, fireEvent, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { RequestsView } from "@/app/(shell)/requests/requests";
import * as fixtures from "@/mock/fixtures";
import { everythingCached, fakeApi, json, renderConsole, type Sent } from "@/tests/render-console";

// The values the API reveals for request 90; they must live in the row's state and nowhere else.
const REVEALED = ["Sam Roe", "555 010 9021", "555 010 7788"];

function api(
  callback: (sent: Sent) => Response = () =>
    json({ source: "requests", id: 90, callbacks: fixtures.CALLBACK_DETAILS[90] }),
) {
  return fakeApi((sent) => {
    if (sent.method === "GET" && sent.path.startsWith("/queue")) {
      return json({ requests: fixtures.QUEUE.filter((row) => row.handled === null) });
    }
    if (sent.method === "POST" && sent.path === "/queue/requests/90/callback") {
      return callback(sent);
    }
    return json({ detail: "Not Found" }, 404);
  });
}

async function showDetails() {
  const buttons = await screen.findAllByRole("button", { name: /Show callback details/ });
  // Request 91's button comes first, then 90's; the stacked rows and the table each have one.
  const forRequest90 = buttons.filter(
    (button) => button.closest("[data-request]")?.getAttribute("data-request") === "90",
  );
  fireEvent.click(forRequest90[0]);
  await screen.findAllByText("Sam Roe");
}

function onPage(value: string) {
  return document.body.textContent?.includes(value) ?? false;
}

describe("callback details", () => {
  const logged: unknown[][] = [];

  beforeEach(() => {
    logged.length = 0;
    for (const method of ["log", "info", "warn", "error", "debug"] as const) {
      vi.spyOn(console, method).mockImplementation((...args: unknown[]) => {
        logged.push(args);
      });
    }
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  function heldNowhereElse(queryClient: Parameters<typeof everythingCached>[0]) {
    const cached = everythingCached(queryClient);
    const stored = JSON.stringify({ ...localStorage }) + JSON.stringify({ ...sessionStorage });
    for (const value of REVEALED) {
      expect(cached, `query cache: ${value}`).not.toContain(value);
      expect(stored, `browser storage: ${value}`).not.toContain(value);
      expect(window.location.href, `address: ${value}`).not.toContain(encodeURIComponent(value));
      expect(JSON.stringify(logged), `console: ${value}`).not.toContain(value);
    }
  }

  it("shows every entry in the row, a missing name as such, and keeps them out of every cache", async () => {
    api();
    const { queryClient } = renderConsole(<RequestsView />);

    await showDetails();

    expect(onPage("Sam Roe")).toBe(true);
    expect(onPage("555 010 9021")).toBe(true);
    expect(onPage("555 010 7788")).toBe(true);
    expect(onPage("No name given")).toBe(true);
    heldNowhereElse(queryClient);
  });

  it("are cleared at once by Hide", async () => {
    api();
    const { queryClient } = renderConsole(<RequestsView />);
    await showDetails();

    fireEvent.click(screen.getAllByRole("button", { name: /Hide/ })[0]);

    for (const value of REVEALED) expect(onPage(value), value).toBe(false);
    heldNowhereElse(queryClient);
  });

  it("are cleared when the list is read again", async () => {
    api();
    const { queryClient } = renderConsole(<RequestsView />);
    await showDetails();

    await act(async () => {
      await queryClient.invalidateQueries({ queryKey: ["queue"] });
    });

    await waitFor(() => expect(onPage("Sam Roe")).toBe(false));
    heldNowhereElse(queryClient);
  });

  it("are cleared after 60 seconds", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    api();
    renderConsole(<RequestsView />);
    await showDetails();
    // Let React run the effects of the render that showed them (it schedules them on a timer).
    await act(async () => {
      vi.advanceTimersByTime(0);
    });

    act(() => {
      vi.advanceTimersByTime(59_000);
    });
    expect(onPage("Sam Roe")).toBe(true);
    act(() => {
      vi.advanceTimersByTime(1_000);
    });

    expect(onPage("Sam Roe")).toBe(false);
  });

  it("leave nothing behind when the page unmounts", async () => {
    api();
    const { queryClient, unmount } = renderConsole(<RequestsView />);
    await showDetails();

    unmount();

    expect(onPage("Sam Roe")).toBe(false);
    heldNowhereElse(queryClient);
  });

  it("a limit says when to try again on the row and reveals nothing", async () => {
    fakeApi((sent) => {
      if (sent.method === "GET")
        return json({ requests: fixtures.QUEUE.filter((row) => row.handled === null) });
      return new Response(JSON.stringify({ detail: "Too many requests" }), {
        status: 429,
        headers: { "Content-Type": "application/json", "Retry-After": "17" },
      });
    });
    const { queryClient } = renderConsole(<RequestsView />);
    const buttons = await screen.findAllByRole("button", { name: /Show callback details/ });

    fireEvent.click(
      buttons.filter((button) => button.closest("[data-request]")?.getAttribute("data-request") === "90")[0],
    );

    expect(
      (await screen.findAllByText("Too many requests. Try again in 17 seconds.")).length,
    ).toBeGreaterThan(0);
    for (const value of REVEALED) expect(onPage(value), value).toBe(false);
    heldNowhereElse(queryClient);
  });

  it("a failure says so on the row and reveals nothing", async () => {
    api(() => json({ detail: "Service unavailable" }, 503));
    const { queryClient } = renderConsole(<RequestsView />);
    const buttons = await screen.findAllByRole("button", { name: /Show callback details/ });

    fireEvent.click(
      buttons.filter((button) => button.closest("[data-request]")?.getAttribute("data-request") === "90")[0],
    );

    expect((await screen.findAllByText("Could not show the details.")).length).toBeGreaterThan(0);
    for (const value of REVEALED) expect(onPage(value), value).toBe(false);
    heldNowhereElse(queryClient);
  });
});
