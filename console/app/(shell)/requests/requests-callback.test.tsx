import { act, fireEvent, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { RequestsView } from "@/app/(shell)/requests/requests";
import * as fixtures from "@/mock/fixtures";
import { everythingCached, fakeApi, json, renderConsole, type Sent } from "@/tests/render-console";

// The values the API reveals for request 90, as it sends them and as the row shows them; they must
// live in the row's state and nowhere else.
const REVEALED = ["Sam Roe", "+1 555 010 9021", "5550107788", "555 010 7788"];
// Request 91's.
const REVEALED_91 = ["Pat Doe", "555 010 3344"];

function details(id: number): Response {
  return json({ source: "requests", id, callbacks: fixtures.CALLBACK_DETAILS[id] });
}

function api(
  callback: (sent: Sent, id: number) => Response | Promise<Response> = (_sent, id) => details(id),
) {
  return fakeApi((sent) => {
    if (sent.method === "GET" && sent.path.startsWith("/queue")) {
      return json({ requests: fixtures.QUEUE.filter((row) => row.handled === null) });
    }
    const asked = /^\/queue\/requests\/(\d+)\/callback$/.exec(sent.path);
    if (sent.method === "POST" && asked) {
      return callback(sent, Number(asked[1]));
    }
    return json({ detail: "Not Found" }, 404);
  });
}

// An answer held until the test lets it go.
function held() {
  let release: () => void = () => {};
  const answer = (response: () => Response) =>
    new Promise<Response>((resolve) => (release = () => resolve(response())));
  return { answer, release: () => release() };
}

function rowOf(id: number): HTMLElement {
  const actions = document.querySelectorAll<HTMLElement>(`[data-request="${id}"]`);
  // The table's row (the stacked list's copy is the other one).
  return actions[actions.length - 1];
}

async function clickShow(id: number) {
  await screen.findAllByRole("button", { name: /Show callback details/ });
  const button = [...rowOf(id).querySelectorAll("button")].find((each) =>
    each.textContent?.includes("Show callback details"),
  );
  fireEvent.click(button!);
}

async function showDetails(id = 90) {
  await clickShow(id);
  await screen.findAllByText(id === 90 ? "Sam Roe" : "Pat Doe");
}

function onPage(value: string) {
  return document.body.textContent?.includes(value) ?? false;
}

function announced(): string {
  return document.getElementById("reveal-status")!.textContent ?? "";
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
    window.history.replaceState(null, "", "/");
  });

  function heldNowhereElse(queryClient: Parameters<typeof everythingCached>[0], values = REVEALED) {
    const cached = everythingCached(queryClient);
    const stored = JSON.stringify({ ...localStorage }) + JSON.stringify({ ...sessionStorage });
    // The address as it reads, however a value was written into it ("+" or "%20" for a space).
    const { pathname, search, hash } = window.location;
    const address = decodeURIComponent(`${pathname}${search}${hash}`.replace(/\+/g, " "));
    for (const value of values) {
      expect(cached, `query cache: ${value}`).not.toContain(value);
      expect(stored, `browser storage: ${value}`).not.toContain(value);
      expect(address, `address: ${value}`).not.toContain(value);
      expect(JSON.stringify(logged), `console: ${value}`).not.toContain(value);
    }
  }

  it("shows every entry in the row, a missing name as such, and keeps them out of every cache", async () => {
    api();
    const { queryClient } = renderConsole(<RequestsView />);

    await showDetails();

    expect(onPage("Sam Roe")).toBe(true);
    expect(onPage("+1 555 010 9021")).toBe(true);
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
    api(
      () =>
        new Response(JSON.stringify({ detail: "Too many requests" }), {
          status: 429,
          headers: { "Content-Type": "application/json", "Retry-After": "17" },
        }),
    );
    const { queryClient } = renderConsole(<RequestsView />);

    await clickShow(90);

    expect(
      (await screen.findAllByText("Too many requests. Try again in 17 seconds.")).length,
    ).toBeGreaterThan(0);
    for (const value of REVEALED) expect(onPage(value), value).toBe(false);
    heldNowhereElse(queryClient);
  });

  it("a failure says so on the row and reveals nothing", async () => {
    api(() => json({ detail: "Service unavailable" }, 503));
    const { queryClient } = renderConsole(<RequestsView />);

    await clickShow(90);

    expect((await screen.findAllByText("Could not show the details.")).length).toBeGreaterThan(0);
    for (const value of REVEALED) expect(onPage(value), value).toBe(false);
    heldNowhereElse(queryClient);
  });
});

describe("one row's details at a time", () => {
  it("showing another row's details hides the first", async () => {
    api();
    renderConsole(<RequestsView />);
    await showDetails(90);

    await showDetails(91);

    for (const value of REVEALED_91) expect(onPage(value), value).toBe(true);
    for (const value of REVEALED) expect(onPage(value), value).toBe(false);
  });

  it("drops an answer for a row replaced by another before it came", async () => {
    const slow = held();
    api((_sent, id) => (id === 90 ? slow.answer(() => details(90)) : details(id)));
    renderConsole(<RequestsView />);

    await clickShow(90);
    await showDetails(91);
    await act(async () => {
      slow.release();
    });

    for (const value of REVEALED_91) expect(onPage(value), value).toBe(true);
    for (const value of REVEALED) expect(onPage(value), value).toBe(false);
  });

  it("drops an answer that comes after the list was read again", async () => {
    const slow = held();
    api(() => slow.answer(() => details(90)));
    const { queryClient } = renderConsole(<RequestsView />);

    await clickShow(90);
    await act(async () => {
      await queryClient.invalidateQueries({ queryKey: ["queue"] });
    });
    await act(async () => {
      slow.release();
    });

    for (const value of REVEALED) expect(onPage(value), value).toBe(false);
    expect(within90("Show callback details")).toBe(true);
  });

  it("drops an answer that comes after the page was left", async () => {
    const slow = held();
    api(() => slow.answer(() => details(90)));
    const { unmount } = renderConsole(<RequestsView />);

    await clickShow(90);
    unmount();
    await act(async () => {
      slow.release();
    });

    for (const value of REVEALED) expect(onPage(value), value).toBe(false);
  });
});

// The text of the focused element when it is a button, else null.
function focusedButton(): string | null {
  const active = document.activeElement;
  return active instanceof HTMLButtonElement ? active.textContent : null;
}

function within90(text: string): boolean {
  return rowOf(90).textContent?.includes(text) ?? false;
}

describe("focus and announcements", () => {
  afterEach(() => {
    vi.useRealTimers();
  });

  it("moves focus to the details when shown, and back to the row's button on Hide", async () => {
    api();
    renderConsole(<RequestsView />);

    await showDetails();

    await waitFor(() => expect(document.activeElement?.getAttribute("aria-label")).toBe("Callback details"));
    expect(document.activeElement?.closest('[data-request="90"]')).not.toBeNull();
    expect(document.activeElement?.getAttribute("tabindex")).toBe("-1");

    fireEvent.click(
      [...rowOf(90).querySelectorAll("button")].find((each) => each.textContent?.includes("Hide"))!,
    );

    await waitFor(() => expect(focusedButton()).toContain("Show callback details"));
    expect(document.activeElement?.closest('[data-request="90"]')).not.toBeNull();
  });

  it("gives focus back to the row's button when the details hide themselves with focus inside", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    api();
    renderConsole(<RequestsView />);
    await showDetails();
    await act(async () => {
      vi.advanceTimersByTime(0);
    });
    await waitFor(() => expect(document.activeElement?.getAttribute("aria-label")).toBe("Callback details"));

    act(() => {
      vi.advanceTimersByTime(60_000);
    });

    await waitFor(() => expect(focusedButton()).toContain("Show callback details"));
    expect(document.activeElement?.closest('[data-request="90"]')).not.toBeNull();
  });

  it("announces shown and hidden in a region there from the start, never the values", async () => {
    api();
    renderConsole(<RequestsView />);
    // The region is in the page, empty, before anything is shown.
    const region = await waitFor(() => document.getElementById("reveal-status")!);
    expect(region.getAttribute("aria-live")).toBe("polite");
    expect(announced()).toBe("");

    await showDetails();
    expect(announced()).toBe("Callback details shown");
    expect(document.getElementById("reveal-status")!).toBe(region);

    fireEvent.click(
      [...rowOf(90).querySelectorAll("button")].find((each) => each.textContent?.includes("Hide"))!,
    );
    expect(announced()).toBe("Callback details hidden");
    expect(document.getElementById("reveal-status")!).toBe(region);
  });
});
