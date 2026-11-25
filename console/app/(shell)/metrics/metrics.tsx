"use client";

import { useState } from "react";
import { Bar, BarChart, CartesianGrid, LabelList, XAxis, YAxis } from "recharts";

import { useClinic } from "@/components/clinic";
import { FilterPills } from "@/components/filter-pills";
import { PageHeader, Section, StatCard } from "@/components/page-header";
import { BlockSkeleton, ErrorState } from "@/components/states";
import { ChartContainer, ChartTooltip, ChartTooltipContent, type ChartConfig } from "@/components/ui/chart";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { useMetrics } from "@/lib/api/hooks";
import type { Metrics } from "@/lib/api/schemas";
import { clinicYmd, percent, plural, stageSeconds, ymdLabel } from "@/lib/format";
import { endReasonLabel, PERIODS, type Period } from "@/lib/labels";

import { LatencyByStage, notEnough, RecordingCost } from "./figures";

const CALLS_CHART: ChartConfig = { calls: { label: "Calls" } };

function CallsPerDay({ metrics, zone }: { metrics: Metrics; zone: string }) {
  const days = metrics.calls_per_day.map((row) => ({
    day: ymdLabel(clinicYmd(row.day, zone), { weekday: "short", day: "numeric" }),
    full: ymdLabel(clinicYmd(row.day, zone), { weekday: "long", month: "long", day: "numeric" }),
    calls: row.calls,
  }));
  const table = (
    <Table>
      <TableHeader>
        <TableRow className="hover:bg-transparent">
          <TableHead className="pl-5">Day</TableHead>
          <TableHead className="pr-5 text-right">Calls</TableHead>
        </TableRow>
      </TableHeader>
      <TableBody>
        {days.map((row) => (
          <TableRow key={row.full}>
            <TableHead scope="row" className="pl-5 font-normal">
              {row.full}
            </TableHead>
            <TableCell className="tabular pr-5 text-right">{row.calls}</TableCell>
          </TableRow>
        ))}
      </TableBody>
    </Table>
  );
  if (days.length === 0) {
    return <p className="px-5 py-5 text-sm text-muted-foreground">No calls in this period.</p>;
  }
  if (days.length < 2) {
    return (
      <>
        <p className="px-5 pt-4 text-sm text-muted-foreground">One day only, so no chart.</p>
        {table}
      </>
    );
  }
  return (
    <div className="grid lg:grid-cols-[minmax(0,1.6fr)_minmax(0,1fr)]">
      <div
        role="img"
        aria-label="Calls started each day. The same numbers are in the table."
        className="px-3 py-4"
      >
        <ChartContainer config={CALLS_CHART} className="aspect-auto h-64 w-full">
          <BarChart data={days} margin={{ top: 20, left: 0, right: 8 }}>
            <CartesianGrid vertical={false} />
            <XAxis
              dataKey="day"
              tickLine={false}
              axisLine={false}
              tickMargin={8}
              interval="preserveStartEnd"
            />
            <YAxis allowDecimals={false} tickLine={false} axisLine={false} width={28} />
            <ChartTooltip cursor={false} content={<ChartTooltipContent hideIndicator labelKey="full" />} />
            <Bar
              dataKey="calls"
              fill="var(--chart-1)"
              radius={[4, 4, 0, 0]}
              maxBarSize={36}
              isAnimationActive={false}
            >
              {days.length <= 10 ? (
                <LabelList dataKey="calls" position="top" className="fill-foreground" fontSize={12} />
              ) : null}
            </Bar>
          </BarChart>
        </ChartContainer>
      </div>
      <div className="border-t lg:border-t-0 lg:border-l">{table}</div>
    </div>
  );
}

function HowCallsEnded({ metrics }: { metrics: Metrics }) {
  const all = metrics.ended.reduce((sum, row) => sum + row.calls, 0);
  if (all === 0) {
    return <p className="px-5 py-5 text-sm text-muted-foreground">No call ended in this period.</p>;
  }
  const rows = [...metrics.ended].sort((a, b) => b.calls - a.calls);
  return (
    <Table>
      <TableHeader>
        <TableRow className="hover:bg-transparent">
          <TableHead className="pl-5">How it ended</TableHead>
          <TableHead className="w-2/5">
            <span className="sr-only">Share</span>
          </TableHead>
          <TableHead className="text-right">Calls</TableHead>
          <TableHead className="pr-5 text-right">Share</TableHead>
        </TableRow>
      </TableHeader>
      <TableBody>
        {rows.map((row) => (
          <TableRow key={row.reason ?? "none"}>
            <TableHead scope="row" className="pl-5 font-normal">
              {endReasonLabel(row.reason)}
            </TableHead>
            <TableCell aria-hidden>
              <div className="h-2.5 rounded-full bg-muted">
                <div
                  className="h-2.5 rounded-full bg-chart-1"
                  style={{ width: `${(row.calls / all) * 100}%` }}
                />
              </div>
            </TableCell>
            <TableCell className="tabular text-right font-medium">{row.calls}</TableCell>
            <TableCell className="tabular pr-5 text-right text-muted-foreground">
              {percent(row.calls, all)}
            </TableCell>
          </TableRow>
        ))}
      </TableBody>
    </Table>
  );
}

function headline(metrics: Metrics) {
  const calls = metrics.calls_per_day.reduce((sum, row) => sum + row.calls, 0);
  const ended = metrics.ended.reduce((sum, row) => sum + row.calls, 0);
  const completed = metrics.ended.find((row) => row.reason === "completed")?.calls ?? 0;
  const wait = metrics.latency.find((row) => row.kind === "plain" && row.stage === "first_audio_ms");
  return { calls, ended, completed, wait };
}

export function MetricsView() {
  const { time_zone: zone } = useClinic();
  const [period, setPeriod] = useState<Period>("7");
  const summary = useMetrics(Number(period));

  let content;
  if (summary.isPending) {
    content = (
      <div className="grid grid-cols-2 gap-3 sm:gap-4 xl:grid-cols-4">
        {[0, 1, 2, 3].map((index) => (
          <BlockSkeleton key={index} className="h-28 w-full" label="Loading" />
        ))}
        <BlockSkeleton className="h-72 w-full sm:col-span-2 xl:col-span-4" label="Loading the charts" />
      </div>
    );
  } else if (summary.isError) {
    content = (
      <Section title="Metrics">
        <ErrorState error={summary.error} onRetry={() => void summary.refetch()} />
      </Section>
    );
  } else {
    const metrics = summary.data;
    const { calls, ended, completed, wait } = headline(metrics);
    content = (
      <div className={summary.isPlaceholderData ? "space-y-6 opacity-60 transition-opacity" : "space-y-6"}>
        <div className="grid grid-cols-2 gap-3 sm:gap-4 xl:grid-cols-4">
          <StatCard label="Calls" value={calls} note={`${plural(ended, "call")} ended in the period`} />
          <StatCard
            label="Finished without staff"
            value={percent(completed, ended)}
            note={`${completed} of ${ended} ended calls completed`}
          />
          <StatCard
            label="Identity checks passed"
            value={percent(metrics.verification_successes, metrics.verification_attempts)}
            note={`${metrics.verification_successes} of ${plural(metrics.verification_attempts, "attempt")}; ${plural(metrics.verified_calls, "call")} verified`}
          />
          <StatCard
            label="Wait to hear the agent"
            value={
              wait?.p50 == null ? (
                <span className="text-base font-medium text-muted-foreground">
                  {notEnough(wait?.values ?? 0)}
                </span>
              ) : (
                stageSeconds(wait.p50)
              )
            }
            note="Median first audio, plain answer"
          />
        </div>
        <Section title="Calls per day" description="Calls started on each day at the clinic.">
          <CallsPerDay metrics={metrics} zone={zone} />
        </Section>
        <Section
          title="How calls ended"
          description="“Finished without staff” counts the completed calls among those that ended."
        >
          <HowCallsEnded metrics={metrics} />
        </Section>
        <Section
          title="Latency by stage"
          description="Median and 95th percentile of each stage of a caller's turn, split by the kind of turn: a plain answer, one that used a tool, or a scripted line the model was not asked for. A figure needs at least 20 turns."
        >
          <LatencyByStage rows={metrics.latency} />
        </Section>
        <Section title="Cost of recording" flush={false}>
          <RecordingCost
            turns={metrics.record_us_turns}
            p50={metrics.record_us_p50}
            dropped={metrics.turn_rows_dropped}
          />
        </Section>
      </div>
    );
  }

  return (
    <>
      <PageHeader
        title="Metrics"
        description="How the agent is doing: how many calls, how many it finished on its own, and how long callers wait to hear it. Every figure is counted from real calls."
        aside={
          summary.data
            ? `From ${ymdLabel(clinicYmd(summary.data.since, zone), { month: "long", day: "numeric" })} to today`
            : null
        }
      />
      <div className="mb-6">
        <FilterPills label="Period" value={period} options={PERIODS} onChange={setPeriod} />
      </div>
      {content}
    </>
  );
}
