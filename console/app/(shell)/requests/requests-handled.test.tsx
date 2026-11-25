import { act, fireEvent, screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { RequestsView } from "@/app/(shell)/requests/requests";
import * as fixtures from "@/mock/fixtures";
import { fakeApi, json, renderConsole } from "@/tests/render-console";

const MARK = { by: "clinic_admin", at: "2026-11-25T17:30:05.123456+00:00" };

const SUMMARY: Record<number, string> = {
  96: "Metformin 500 mg at Walgreens Oak Ave",
  95: "Lisinopril 10 mg at CVS Main St",
};

// The answers to "Mark handled", by request id; each is called when its request is sent.
function api(answers: Record<number, () => Promise<Response>>) {
  return fakeApi((sent) => {
    if (sent.method === "GET" && sent.path.startsWith("/queue")) {
      return json({ requests: fixtures.QUEUE.filter((row) => row.handled === null) });
    }
    const handled = /^\/queue\/requests\/(\d+)\/handled$/.exec(sent.path);
    if (sent.method === "POST" && handled && answers[Number(handled[1])]) {
      return answers[Number(handled[1])]();
    }
    return json({ detail: "Not Found" }, 404);
  });
}

// An answer held until the test lets it go.
function held() {
  let release: (response: Response) => void = () => {};
  const answer = () => new Promise<Response>((resolve) => (release = resolve));
  return { answer, release: (response: Response) => release(response) };
}

function rowOf(id: number): HTMLElement {
  const actions = document.querySelectorAll<HTMLElement>(`[data-request="${id}"]`);
  // The table's row (the stacked list's copy is the other one).
  return actions[actions.length - 1];
}

async function markAndConfirm(id = 96) {
  await screen.findAllByText(SUMMARY[id]);
  fireEvent.click(within(rowOf(id)).getByRole("button", { name: "Mark handled" }));
  const dialog = await screen.findByRole("alertdialog");
  fireEvent.click(within(dialog).getByRole("button", { name: "Mark handled" }));
  await waitFor(() => expect(screen.queryByRole("alertdialog")).toBeNull());
}

describe("Mark handled", () => {
  it("changes the row only once the API has answered", async () => {
    const answer = held();
    api({ 96: answer.answer });
    renderConsole(<RequestsView />);

    await markAndConfirm();

    // Sent, not answered: the row is as it was.
    expect(within(rowOf(96)).queryByText("Handled")).toBeNull();
    expect(within(rowOf(96)).getByText("Marking…")).toBeTruthy();

    await act(async () => {
      answer.release(json({ source: "requests", id: 96, handled: MARK }, 201));
    });

    expect(await within(rowOf(96)).findByText("Handled")).toBeTruthy();
    expect(within(rowOf(96)).getByText(/by clinic_admin at/)).toBeTruthy();
    expect(within(rowOf(96)).queryByRole("button", { name: "Mark handled" })).toBeNull();
  });

  it("shows a mark the request already had as the same result, not as an error", async () => {
    api({ 96: async () => json({ source: "requests", id: 96, handled: MARK }, 200) });
    renderConsole(<RequestsView />);

    await markAndConfirm();

    expect(await within(rowOf(96)).findByText("Handled")).toBeTruthy();
    expect(within(rowOf(96)).queryByRole("alert")).toBeNull();
  });

  it("leaves the row as it was when the API refuses, and says so on the row", async () => {
    api({ 96: async () => json({ detail: "Service unavailable" }, 503) });
    renderConsole(<RequestsView />);

    await markAndConfirm();

    expect(await within(rowOf(96)).findByText("Could not mark it handled. Nothing changed.")).toBeTruthy();
    expect(within(rowOf(96)).queryByText("Handled")).toBeNull();
    expect(within(rowOf(96)).getByRole("button", { name: "Mark handled" })).toBeTruthy();
  });

  it("says the session has changed when the anti-forgery check refuses it", async () => {
    api({ 96: async () => json({ detail: "Forbidden" }, 403) });
    renderConsole(<RequestsView />);

    await markAndConfirm();

    expect(await within(rowOf(96)).findByText("Your session has changed. Sign in again.")).toBeTruthy();
    expect(within(rowOf(96)).queryByText("Handled")).toBeNull();
  });
});

describe("two marks on their way at once", () => {
  it("each row shows its own answer when both succeed", async () => {
    const first = held();
    const second = held();
    api({ 96: first.answer, 95: second.answer });
    renderConsole(<RequestsView />);

    await markAndConfirm(96);
    await markAndConfirm(95);
    expect(within(rowOf(96)).getByText("Marking…")).toBeTruthy();
    expect(within(rowOf(95)).getByText("Marking…")).toBeTruthy();

    await act(async () => {
      first.release(json({ source: "requests", id: 96, handled: MARK }, 201));
    });
    await act(async () => {
      second.release(json({ source: "requests", id: 95, handled: MARK }, 201));
    });

    expect(await within(rowOf(96)).findByText("Handled")).toBeTruthy();
    expect(await within(rowOf(95)).findByText("Handled")).toBeTruthy();
  });

  it("a failure stays on its own row while the other is handled", async () => {
    const first = held();
    const second = held();
    api({ 96: first.answer, 95: second.answer });
    renderConsole(<RequestsView />);

    await markAndConfirm(96);
    await markAndConfirm(95);
    await act(async () => {
      first.release(json({ detail: "Service unavailable" }, 503));
    });
    await act(async () => {
      second.release(json({ source: "requests", id: 95, handled: MARK }, 201));
    });

    expect(await within(rowOf(96)).findByText("Could not mark it handled. Nothing changed.")).toBeTruthy();
    expect(within(rowOf(96)).queryByText("Handled")).toBeNull();
    expect(within(rowOf(96)).getByRole("button", { name: "Mark handled" })).toBeTruthy();
    expect(await within(rowOf(95)).findByText("Handled")).toBeTruthy();
    expect(within(rowOf(95)).queryByRole("alert")).toBeNull();
  });
});

describe("focus after Mark handled", () => {
  it("never rests on a disabled button, and lands on the row's handled text", async () => {
    const answer = held();
    api({ 96: answer.answer });
    renderConsole(<RequestsView />);

    await markAndConfirm();
    // Past the dialog's own focus return.
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 10));
    });

    expect(document.activeElement).not.toBe(document.body);
    expect((document.activeElement as HTMLButtonElement).disabled ?? false).toBe(false);
    expect(document.activeElement?.closest('[data-request="96"]')).not.toBeNull();

    await act(async () => {
      answer.release(json({ source: "requests", id: 96, handled: MARK }, 201));
    });

    await waitFor(() => expect(document.activeElement?.textContent).toContain("Handled"));
    expect(document.activeElement?.closest('[data-request="96"]')).not.toBeNull();
  });
});
