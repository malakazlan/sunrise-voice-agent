import { fireEvent, render, screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { LatencyByStage, RecordingCost } from "@/app/(shell)/metrics/figures";
import type { LatencyRow } from "@/lib/api/schemas";

const STAGES = ["vad_ms", "stt_ms", "first_token_ms", "last_token_ms", "first_audio_ms", "total_ms"] as const;

function rows(kind: LatencyRow["kind"], turns: number, figure: [number, number] | null): LatencyRow[] {
  return STAGES.map((stage) => ({
    kind,
    turns,
    stage,
    values: turns,
    p50: figure === null ? null : figure[0],
    p95: figure === null ? null : figure[1],
  }));
}

const LATENCY = [
  ...rows("scripted", 12, null),
  ...rows("plain", 148, [1790, 2620]),
  ...rows("tool", 0, null),
];
// A figure, however it is written: a number with or without a unit.
const FIGURE = /\d[\d,.]*\s*(?:ms|s|µs)?/;

function cellsOf(rowName: RegExp): string[] {
  const table = screen.getByRole("table");
  const row = within(table).getByRole("row", { name: rowName });
  return within(row)
    .getAllByRole("cell")
    .map((cell) => cell.textContent ?? "");
}

describe("metrics without enough figures", () => {
  it("a kind of turn with too few turns shows 'Not enough calls yet', never a number", () => {
    render(<LatencyByStage rows={LATENCY} />);

    fireEvent.click(screen.getByRole("radio", { name: /Scripted/ }));

    for (const stage of [/Silence wait/, /Transcript/, /Whole turn/]) {
      const cells = cellsOf(stage);
      expect(cells).toEqual(["Not enough calls yet (12 turns)", "Not enough calls yet (12 turns)"]);
    }
  });

  it("a kind with no turns at all says so, and nothing else", () => {
    render(<LatencyByStage rows={LATENCY} />);

    fireEvent.click(screen.getByRole("radio", { name: /tool/i }));

    const cells = cellsOf(/First audio/);
    expect(cells).toEqual(["Not enough calls yet (0 turns)", "Not enough calls yet (0 turns)"]);
    expect(screen.queryByRole("img")).toBeNull();
  });

  it("a kind with figures shows them", () => {
    render(<LatencyByStage rows={LATENCY} />);

    const cells = cellsOf(/First audio/);
    expect(cells).toEqual(["1.79 s", "2.62 s"]);
  });

  it("the cost of recording a turn without enough turns shows no number", () => {
    render(<RecordingCost turns={7} p50={null} dropped={0} />);

    const figure = screen.getByTestId("recording-cost");
    expect(figure.textContent).toBe("Not enough calls yet (7 turns)");
    expect(figure.textContent?.replace("(7 turns)", "")).not.toMatch(FIGURE);
  });
});
