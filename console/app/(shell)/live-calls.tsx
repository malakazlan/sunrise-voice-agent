"use client";

import { useQueryClient } from "@tanstack/react-query";
import { ArrowRight, PhoneOff, TriangleAlert } from "lucide-react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useCallback } from "react";

import { LivePulse } from "@/components/app-shell";
import { useClinic } from "@/components/clinic";
import { PageHeader, Section } from "@/components/page-header";
import { CallerBadge, PhaseBadge } from "@/components/status-badge";
import { EmptyState, ErrorState, TableSkeleton } from "@/components/states";
import { Button } from "@/components/ui/button";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { ApiError } from "@/lib/api/client";
import { useLiveCalls, type LiveCalls } from "@/lib/api/hooks";
import type { LiveCall } from "@/lib/api/schemas";
import {
  clinicTime,
  clinicTimeSeconds,
  duration,
  plural,
  secondsBetween,
  secondsFigure,
  shortId,
  stageSeconds,
} from "@/lib/format";
import { useNow } from "@/lib/use-now";

const TIMING_COLUMNS = [
  ["vad_ms", "Silence wait"],
  ["stt_ms", "Transcript"],
  ["first_token_ms", "First token"],
  ["first_audio_ms", "First audio"],
] as const;

function StreamStatus({ live, zone }: { live: LiveCalls; zone: string }) {
  const since = live.updatedAt === null ? null : clinicTimeSeconds(live.updatedAt, zone);
  let text: string;
  switch (live.state) {
    case "live":
      text = `Updated ${since}`;
      break;
    case "connecting":
      text = since === null ? "Connecting…" : `Reconnecting… Showing the list from ${since}`;
      break;
    case "paused":
    case "retrying":
      text =
        since === null
          ? "Live updates paused — retrying"
          : `Live updates paused — retrying. Showing the list from ${since}`;
      break;
    case "invalid":
      text = "A live update could not be read and was not shown";
      break;
  }
  return (
    <p role="status" className="flex flex-wrap items-center gap-x-2 md:justify-end">
      <span className="inline-flex items-center gap-2 font-medium whitespace-nowrap text-foreground">
        <LivePulse active={live.state === "live"} />
        {live.state === "live" ? "Live" : "Not live"}
      </span>
      <span>{text}</span>
    </p>
  );
}

function EmergencyBanner({ call, now, zone }: { call: LiveCall; now: Date; zone: string }) {
  return (
    <div
      role="alert"
      className="flex flex-col gap-4 rounded-xl border border-emergency/30 border-l-4 border-l-emergency bg-emergency-soft px-5 py-4 sm:flex-row sm:items-center"
    >
      <span className="flex size-10 shrink-0 items-center justify-center rounded-full bg-emergency text-white">
        <TriangleAlert className="size-5" aria-hidden />
      </span>
      <div className="min-w-0 flex-1">
        <p className="font-heading font-semibold text-emergency">
          Emergency on call <span className="font-mono text-sm">{shortId(call.call_id)}…</span>
        </p>
        <p className="text-sm">
          The agent plays the 911 line and ends the call. Started {clinicTime(call.started_at, zone)}, running
          for {duration(secondsBetween(call.started_at, now))}.
        </p>
      </div>
      <Button asChild variant="outline" className="bg-card">
        <Link href={`/audit?call_id=${call.call_id}`}>
          See it in the audit log
          <ArrowRight aria-hidden />
        </Link>
      </Button>
    </div>
  );
}

function LiveList({ calls, now, zone }: { calls: LiveCall[]; now: Date; zone: string }) {
  return (
    <ul className="divide-y sm:hidden">
      {calls.map((call) => (
        <li
          key={call.call_id}
          className={`space-y-2 px-5 py-4 ${call.phase === "emergency" ? "bg-emergency-soft/50" : ""}`}
        >
          <div className="flex items-center justify-between gap-3">
            <span title={call.call_id} className="font-mono text-xs">
              {shortId(call.call_id)}…
            </span>
            <PhaseBadge phase={call.phase} />
          </div>
          <p className="tabular text-sm">
            Started {clinicTime(call.started_at, zone)} · running{" "}
            {duration(secondsBetween(call.started_at, now))}
          </p>
          <div className="flex flex-wrap items-center gap-x-3 gap-y-1 text-sm">
            <CallerBadge verified={call.verified} patientId={call.patient_id} />
            <span className="font-mono text-xs text-muted-foreground">{call.last_tool ?? "no tool yet"}</span>
          </div>
          <dl className="grid grid-cols-4 gap-2 rounded-md bg-muted/60 px-3 py-2 text-xs">
            {TIMING_COLUMNS.map(([key, label]) => {
              const value = call[key];
              return (
                <div key={key}>
                  <dt className="text-muted-foreground">{label}</dt>
                  <dd className="tabular font-medium">{value === null ? "–" : stageSeconds(value)}</dd>
                </div>
              );
            })}
          </dl>
        </li>
      ))}
    </ul>
  );
}

function LiveTable({ calls, now, zone }: { calls: LiveCall[]; now: Date; zone: string }) {
  return (
    <Table className="hidden sm:table">
      <TableHeader>
        <TableRow className="hover:bg-transparent">
          <TableHead className="pl-5">Call</TableHead>
          <TableHead>Started</TableHead>
          <TableHead>Running for</TableHead>
          <TableHead>Phase</TableHead>
          <TableHead>Caller</TableHead>
          <TableHead>Last tool</TableHead>
          {TIMING_COLUMNS.map(([key, label], index) => (
            <TableHead
              key={key}
              className={`text-right ${index === 0 ? "border-l" : ""} ${index === 3 ? "pr-5" : ""}`}
            >
              {label}
            </TableHead>
          ))}
        </TableRow>
      </TableHeader>
      <TableBody>
        {calls.map((call) => (
          <TableRow
            key={call.call_id}
            className={call.phase === "emergency" ? "bg-emergency-soft/50" : undefined}
          >
            <TableCell className="pl-5">
              <span title={call.call_id} className="font-mono text-xs">
                {shortId(call.call_id)}…
              </span>
            </TableCell>
            <TableCell className="tabular">{clinicTime(call.started_at, zone)}</TableCell>
            <TableCell className="tabular">{duration(secondsBetween(call.started_at, now))}</TableCell>
            <TableCell>
              <PhaseBadge phase={call.phase} />
            </TableCell>
            <TableCell>
              <CallerBadge verified={call.verified} patientId={call.patient_id} />
            </TableCell>
            <TableCell className="font-mono text-xs">{call.last_tool ?? "–"}</TableCell>
            {TIMING_COLUMNS.map(([key], index) => {
              const value = call[key];
              return (
                <TableCell
                  key={key}
                  className={`tabular text-right ${index === 0 ? "border-l" : ""} ${index === 3 ? "pr-5 font-semibold" : ""}`}
                >
                  {value === null ? "–" : secondsFigure(value)}
                </TableCell>
              );
            })}
          </TableRow>
        ))}
      </TableBody>
    </Table>
  );
}

export function LiveCallsView() {
  const { time_zone: zone } = useClinic();
  const router = useRouter();
  const queryClient = useQueryClient();
  const onSignedOut = useCallback(() => {
    queryClient.clear();
    router.replace("/login");
  }, [queryClient, router]);
  const live = useLiveCalls(onSignedOut);
  const now = useNow(1000);
  const emergencies = live.calls?.filter((call) => call.phase === "emergency") ?? [];

  let body;
  if (live.calls === null) {
    body =
      live.state === "invalid" ? (
        <ErrorState error={new ApiError("invalid_answer")} />
      ) : (
        <TableSkeleton label="Waiting for the live calls" rows={3} />
      );
  } else if (live.calls.length === 0) {
    body = (
      <EmptyState icon={PhoneOff} title="No calls in progress">
        A call appears here when it starts, and moves to Call history when it ends.
      </EmptyState>
    );
  } else {
    body = (
      <>
        <LiveList calls={live.calls} now={now} zone={zone} />
        <LiveTable calls={live.calls} now={now} zone={zone} />
      </>
    );
  }

  return (
    <>
      <PageHeader
        title="Live calls"
        description="Calls the agent is on right now. This page never plays audio and never shows what is being said."
        aside={<StreamStatus live={live} zone={zone} />}
      />
      <div className="space-y-6">
        {emergencies.map((call) => (
          <EmergencyBanner key={call.call_id} call={call} now={now} zone={zone} />
        ))}
        <Section
          title="In progress"
          description={
            live.calls === null
              ? "Calls leave this list when they end and appear in Call history."
              : `${plural(live.calls.length, "call")} in progress. The timings are the caller's last turn, in seconds.`
          }
        >
          {body}
        </Section>
        <p className="flex flex-wrap items-center justify-between gap-2 text-sm text-muted-foreground">
          <span>
            Patients appear as ids only. Names, dates of birth and phone numbers are never shown here.
          </span>
          <Link
            href="/calls"
            className="inline-flex items-center gap-1 font-medium text-primary hover:underline"
          >
            A call that ends moves to Call history
            <ArrowRight className="size-4" aria-hidden />
          </Link>
        </p>
      </div>
    </>
  );
}
