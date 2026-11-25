"use client";

import { useState } from "react";
import { Bar, BarChart, CartesianGrid, XAxis, YAxis } from "recharts";

import { FilterPills } from "@/components/filter-pills";
import {
  ChartContainer,
  ChartLegend,
  ChartLegendContent,
  ChartTooltip,
  ChartTooltipContent,
  type ChartConfig,
} from "@/components/ui/chart";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { STAGES, type LatencyRow, type TurnKind } from "@/lib/api/schemas";
import { plural, secondsFigure, stageSeconds } from "@/lib/format";
import { STAGE_HINT, STAGE_LABEL, TURN_KIND_LABEL } from "@/lib/labels";

// The API leaves a figure out when it has fewer values than it needs; the console then says so
// and shows the count it had, never a number in the figure's place.
export function notEnough(values: number): string {
  return `Not enough calls yet (${plural(values, "turn")})`;
}

const KIND_ORDER: TurnKind[] = ["plain", "tool", "scripted"];

const LATENCY_CHART: ChartConfig = {
  p50: { label: "Median" },
  p95: { label: "95th percentile" },
};

export function LatencyByStage({ rows }: { rows: LatencyRow[] }) {
  const [kind, setKind] = useState<TurnKind>("plain");
  const ofKind = STAGES.map((stage) => rows.find((row) => row.kind === kind && row.stage === stage)).filter(
    (row): row is LatencyRow => row !== undefined,
  );
  const turns = (each: TurnKind) => rows.find((row) => row.kind === each)?.turns ?? 0;
  const charted = ofKind
    .filter((row) => row.p50 !== null && row.p95 !== null)
    .map((row) => ({
      stage: STAGE_LABEL[row.stage],
      p50: (row.p50 ?? 0) / 1000,
      p95: (row.p95 ?? 0) / 1000,
    }));

  return (
    <div>
      <div className="border-b px-5 py-4">
        <FilterPills
          label="Kind of turn"
          value={kind}
          onChange={setKind}
          options={KIND_ORDER.map((each) => ({
            value: each,
            label: `${TURN_KIND_LABEL[each]} · ${plural(turns(each), "turn")}`,
          }))}
        />
      </div>
      <div className="grid gap-0 lg:grid-cols-[minmax(0,1fr)_minmax(0,1.1fr)]">
        <Table>
          <TableHeader>
            <TableRow className="hover:bg-transparent">
              <TableHead className="pl-5">Stage</TableHead>
              <TableHead className="text-right">Median</TableHead>
              <TableHead className="pr-5 text-right">95th percentile</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {ofKind.map((row) => (
              <TableRow key={row.stage}>
                <TableHead scope="row" className="h-auto py-2 pl-5 font-normal whitespace-normal">
                  <span className="block font-medium">{STAGE_LABEL[row.stage]}</span>
                  <span className="block text-xs text-muted-foreground">{STAGE_HINT[row.stage]}</span>
                </TableHead>
                {[row.p50, row.p95].map((figure, index) => (
                  <TableCell
                    key={index}
                    className={`text-right ${index === 1 ? "pr-5" : ""} ${figure === null ? "min-w-44 text-xs whitespace-normal text-muted-foreground" : "tabular font-medium"}`}
                  >
                    {figure === null ? notEnough(row.values) : stageSeconds(figure)}
                  </TableCell>
                ))}
              </TableRow>
            ))}
          </TableBody>
        </Table>
        {charted.length >= 2 ? (
          <div
            role="img"
            aria-label={`Median and 95th percentile of each stage for ${TURN_KIND_LABEL[kind].toLowerCase()} turns, in seconds. The same figures are in the table.`}
            className="border-t px-3 py-4 lg:border-t-0 lg:border-l"
          >
            <ChartContainer config={LATENCY_CHART} className="aspect-auto h-80 w-full">
              <BarChart
                data={charted}
                layout="vertical"
                margin={{ left: 8, right: 16, top: 4, bottom: 4 }}
                barGap={2}
              >
                <CartesianGrid horizontal={false} />
                <XAxis
                  type="number"
                  tickLine={false}
                  axisLine={false}
                  tickFormatter={(value: number) => `${value} s`}
                />
                <YAxis type="category" dataKey="stage" tickLine={false} axisLine={false} width={92} />
                <ChartTooltip
                  cursor={false}
                  content={
                    <ChartTooltipContent
                      formatter={(value, name) =>
                        `${LATENCY_CHART[String(name)]?.label}: ${secondsFigure(Number(value) * 1000)} s`
                      }
                    />
                  }
                />
                <ChartLegend content={<ChartLegendContent />} />
                <Bar
                  dataKey="p50"
                  fill="var(--chart-1)"
                  radius={[0, 4, 4, 0]}
                  barSize={10}
                  isAnimationActive={false}
                />
                <Bar
                  dataKey="p95"
                  fill="var(--chart-2)"
                  radius={[0, 4, 4, 0]}
                  barSize={10}
                  isAnimationActive={false}
                />
              </BarChart>
            </ChartContainer>
          </div>
        ) : (
          <p className="border-t px-5 py-5 text-sm text-muted-foreground lg:border-t-0 lg:border-l">
            No chart: fewer than two stages have figures for this kind of turn.
          </p>
        )}
      </div>
    </div>
  );
}

export function RecordingCost({
  turns,
  p50,
  dropped,
}: {
  turns: number;
  p50: number | null;
  dropped: number;
}) {
  return (
    <div className="space-y-2">
      <p className="font-heading text-2xl font-semibold tracking-tight" data-testid="recording-cost">
        {p50 === null ? (
          <span className="text-base font-medium text-muted-foreground">{notEnough(turns)}</span>
        ) : (
          `${p50} µs`
        )}
      </p>
      <p className="text-sm text-muted-foreground">
        The median time a turn spends handing its timings to the recorder, over {plural(turns, "turn")}. The
        recorder writes them after the turn, off the audio path.
      </p>
      <p className="text-sm">
        {dropped === 0
          ? "No timing rows dropped since the voice service started."
          : `${plural(dropped, "timing row")} dropped since the voice service started.`}
      </p>
    </div>
  );
}
