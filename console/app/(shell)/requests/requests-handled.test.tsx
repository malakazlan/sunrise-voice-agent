import { act, fireEvent, screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { RequestsView } from "@/app/(shell)/requests/requests";
import * as fixtures from "@/mock/fixtures";
import { fakeApi, json, renderConsole } from "@/tests/render-console";

const MARK = { by: "clinic_admin", at: "2026-11-25T17:30:05.123456+00:00" };

// The answer to "Mark handled" for request 96, held until the test lets it go.
function api(answer: () => Promise<Response>) {
  return fakeApi((sent) => {
    if (sent.method === "GET" && sent.path.startsWith("/queue")) {
      return json({ requests: fixtures.QUEUE.filter((row) => row.handled === null) });
    }
    if (sent.method === "POST" && sent.path === "/queue/requests/96/handled") {
      return answer();
    }
    return json({ detail: "Not Found" }, 404);
  });
}

function rowOf96(): HTMLElement {
  const actions = document.querySelectorAll<HTMLElement>('[data-request="96"]');
  // The table's row (the stacked list's copy is the other one).
  return actions[actions.length - 1];
}

async function markAndConfirm() {
  await screen.findAllByText("Metformin 500 mg at Walgreens Oak Ave");
  fireEvent.click(within(rowOf96()).getByRole("button", { name: "Mark handled" }));
  const dialog = await screen.findByRole("alertdialog");
  fireEvent.click(within(dialog).getByRole("button", { name: "Mark handled" }));
}

describe("Mark handled", () => {
  it("changes the row only once the API has answered", async () => {
    let release: (response: Response) => void = () => {};
    api(() => new Promise<Response>((resolve) => (release = resolve)));
    renderConsole(<RequestsView />);

    await markAndConfirm();

    // Sent, not answered: the row is as it was.
    expect(within(rowOf96()).queryByText("Handled")).toBeNull();
    expect(within(rowOf96()).getByRole("button", { name: /Marking/ })).toBeTruthy();

    await act(async () => {
      release(json({ source: "requests", id: 96, handled: MARK }, 201));
    });

    expect(await within(rowOf96()).findByText("Handled")).toBeTruthy();
    expect(within(rowOf96()).getByText(/by clinic_admin at/)).toBeTruthy();
    expect(within(rowOf96()).queryByRole("button", { name: "Mark handled" })).toBeNull();
  });

  it("shows a mark the request already had as the same result, not as an error", async () => {
    api(async () => json({ source: "requests", id: 96, handled: MARK }, 200));
    renderConsole(<RequestsView />);

    await markAndConfirm();

    expect(await within(rowOf96()).findByText("Handled")).toBeTruthy();
    expect(within(rowOf96()).queryByRole("alert")).toBeNull();
  });

  it("leaves the row as it was when the API refuses, and says so on the row", async () => {
    api(async () => json({ detail: "Service unavailable" }, 503));
    renderConsole(<RequestsView />);

    await markAndConfirm();

    expect(await within(rowOf96()).findByText("Could not mark it handled. Nothing changed.")).toBeTruthy();
    expect(within(rowOf96()).queryByText("Handled")).toBeNull();
    expect(within(rowOf96()).getByRole("button", { name: "Mark handled" })).toBeTruthy();
  });

  it("says the session has changed when the anti-forgery check refuses it", async () => {
    api(async () => json({ detail: "Forbidden" }, 403));
    renderConsole(<RequestsView />);

    await markAndConfirm();

    expect(await within(rowOf96()).findByText("Your session has changed. Sign in again.")).toBeTruthy();
    expect(within(rowOf96()).queryByText("Handled")).toBeNull();
  });
});
