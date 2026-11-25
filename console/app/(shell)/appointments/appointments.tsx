"use client";

import { ArrowLeft, ArrowRight, CalendarDays, CalendarX, Phone, UserRound } from "lucide-react";
import { Fragment, useMemo, useState } from "react";

import { CallLink } from "@/components/call-link";
import { useClinic } from "@/components/clinic";
import { MaskedText } from "@/components/masked-text";
import { PageHeader, Section, StatCard } from "@/components/page-header";
import { StatusPill } from "@/components/status-badge";
import { BlockSkeleton, EmptyState, ErrorState, ShowMore, TableSkeleton } from "@/components/states";
import { Button } from "@/components/ui/button";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { allRows, SLOT_DAYS, useAppointments, useSlots } from "@/lib/api/hooks";
import type { Appointment, Slot } from "@/lib/api/schemas";
import { addDays, clinicDateTime, clinicTime, clinicYmd, mondayOf, plural, ymdLabel } from "@/lib/format";
import { useNow } from "@/lib/use-now";

interface Cell {
  slot: Slot;
  appointment: Appointment | undefined;
}

function SlotCell({ cell }: { cell: Cell | undefined }) {
  const base = "flex min-h-12 flex-col items-center justify-center rounded-md px-2 py-1 text-xs font-medium";
  if (cell === undefined) {
    return <div className={`${base} border border-dashed text-muted-foreground`}>No slot</div>;
  }
  if (cell.slot.available) {
    return <div className={`${base} border border-primary/40 bg-card text-primary`}>Open</div>;
  }
  if (cell.appointment?.booked_by_call) {
    return (
      <div className={`${base} bg-primary text-primary-foreground`}>
        <span className="inline-flex items-center gap-1">
          <Phone className="size-3.5" aria-hidden />
          Agent booked
        </span>
        <span className="font-mono text-[0.7rem] opacity-90">{cell.appointment.patient_id}</span>
      </div>
    );
  }
  return (
    <div className={`${base} bg-muted text-foreground`}>
      <span className="inline-flex items-center gap-1">
        <UserRound className="size-3.5" aria-hidden />
        Taken
      </span>
      {cell.appointment ? (
        <span className="font-mono text-[0.7rem] text-muted-foreground">{cell.appointment.patient_id}</span>
      ) : null}
    </div>
  );
}

function Legend() {
  const chip = "inline-flex items-center gap-1 rounded-md px-2 py-0.5 text-xs font-medium";
  return (
    <ul
      className="flex flex-wrap gap-x-4 gap-y-2 text-xs text-muted-foreground"
      aria-label="What the cells mean"
    >
      <li className="flex items-center gap-1.5">
        <span className={`${chip} border border-primary/40 text-primary`}>Open</span> free to book
      </li>
      <li className="flex items-center gap-1.5">
        <span className={`${chip} bg-muted text-foreground`}>
          <UserRound className="size-3" aria-hidden />
          Taken
        </span>
        booked, not by the agent (or not among the bookings loaded)
      </li>
      <li className="flex items-center gap-1.5">
        <span className={`${chip} bg-primary text-primary-foreground`}>
          <Phone className="size-3" aria-hidden />
          Agent booked
        </span>
        taken by the agent on a call
      </li>
      <li className="flex items-center gap-1.5">
        <span className={`${chip} border border-dashed`}>No slot</span> the doctor is not in
      </li>
    </ul>
  );
}

function SlotGrid({
  slots,
  appointments,
  days,
  zone,
}: {
  slots: Slot[];
  appointments: Appointment[];
  days: string[];
  zone: string;
}) {
  const doctors = [...new Set(slots.map((slot) => slot.doctor))].sort();
  const byKey = new Map<string, Cell>();
  for (const slot of slots) {
    const appointment = appointments.find(
      (each) => each.doctor === slot.doctor && Date.parse(each.starts_at) === Date.parse(slot.starts_at),
    );
    byKey.set(`${slot.doctor}|${clinicYmd(slot.starts_at, zone)}|${clinicTime(slot.starts_at, zone)}`, {
      slot,
      appointment,
    });
  }
  return (
    <Table className="min-w-[44rem] table-fixed">
      <TableHeader>
        <TableRow className="hover:bg-transparent">
          <TableHead className="w-24 pl-5">Time</TableHead>
          {days.map((day) => (
            <TableHead key={day} className="text-center">
              <span className="block text-xs font-semibold uppercase">
                {ymdLabel(day, { weekday: "short" })}
              </span>
              <span className="block text-xs font-normal text-muted-foreground">
                {ymdLabel(day, { month: "short", day: "numeric" })}
              </span>
            </TableHead>
          ))}
        </TableRow>
      </TableHeader>
      <TableBody>
        {doctors.map((doctor) => {
          const mine = slots.filter((slot) => slot.doctor === doctor);
          const times = [...new Set(mine.map((slot) => clinicTime(slot.starts_at, zone)))].sort(
            (a, b) => minutesOf(a) - minutesOf(b),
          );
          const open = mine.filter((slot) => slot.available).length;
          return (
            <Fragment key={doctor}>
              <TableRow className="bg-muted/50 hover:bg-muted/50">
                <TableCell colSpan={days.length + 1} className="pl-5">
                  <span className="font-heading font-semibold">{doctor}</span>
                  <span className="ml-3 text-xs text-muted-foreground">
                    {open} open · {mine.length - open} taken
                  </span>
                </TableCell>
              </TableRow>
              {times.map((time) => (
                <TableRow key={time} className="hover:bg-transparent">
                  <TableHead scope="row" className="pl-5 font-medium tabular">
                    {time}
                  </TableHead>
                  {days.map((day) => (
                    <TableCell key={day} className="px-1.5 py-1.5">
                      <SlotCell cell={byKey.get(`${doctor}|${day}|${time}`)} />
                    </TableCell>
                  ))}
                </TableRow>
              ))}
            </Fragment>
          );
        })}
      </TableBody>
    </Table>
  );
}

function minutesOf(time: string): number {
  const match = /^(\d+):(\d+)\s*(AM|PM)$/.exec(time);
  if (match === null) return 0;
  const hours = (Number(match[1]) % 12) + (match[3] === "PM" ? 12 : 0);
  return hours * 60 + Number(match[2]);
}

export function AppointmentsView() {
  const { time_zone: zone } = useClinic();
  const now = useNow(60_000);
  const [weekStart, setWeekStart] = useState(() => mondayOf(clinicYmd(new Date(), zone)));
  const slots = useSlots(weekStart);
  const appointments = useAppointments();
  const booked = allRows(appointments.data);

  const weekDays = useMemo(() => {
    const all = Array.from({ length: SLOT_DAYS }, (_, index) => addDays(weekStart, index));
    const withSlots = new Set((slots.data?.slots ?? []).map((slot) => clinicYmd(slot.starts_at, zone)));
    return all.filter((day, index) => index < 5 || withSlots.has(day));
  }, [weekStart, slots.data, zone]);

  const weekSlots = slots.data?.slots ?? [];
  const open = weekSlots.filter((slot) => slot.available);
  const next = open.find((slot) => Date.parse(slot.starts_at) > now.getTime());
  const inWeek = booked.filter((each) => weekDays.includes(clinicYmd(each.starts_at, zone)));
  const range = `${ymdLabel(weekStart, { month: "long", day: "numeric" })} to ${ymdLabel(addDays(weekStart, SLOT_DAYS - 1), { month: "long", day: "numeric", year: "numeric" })}`;

  let grid;
  if (slots.isPending) {
    grid = <TableSkeleton label="Loading the slots" />;
  } else if (slots.isError) {
    grid = <ErrorState error={slots.error} onRetry={() => void slots.refetch()} />;
  } else if (weekSlots.length === 0) {
    grid = (
      <EmptyState icon={CalendarX} title="No slots this week">
        No doctor has times set for this week. Try another week.
      </EmptyState>
    );
  } else {
    grid = (
      <div className={slots.isPlaceholderData ? "opacity-60 transition-opacity" : undefined}>
        <SlotGrid slots={weekSlots} appointments={booked} days={weekDays} zone={zone} />
        {slots.data?.truncated ? (
          <p className="border-t px-5 py-3 text-sm text-muted-foreground">
            This week has more slots than one read returns; only the first ones are shown.
          </p>
        ) : null}
      </div>
    );
  }

  let list;
  if (appointments.isPending) {
    list = <TableSkeleton label="Loading the appointments" />;
  } else if (appointments.isError) {
    list = <ErrorState error={appointments.error} onRetry={() => void appointments.refetch()} />;
  } else if (booked.length === 0) {
    list = <EmptyState icon={CalendarDays} title="No appointments booked" />;
  } else {
    list = (
      <>
        <Table>
          <TableHeader>
            <TableRow className="hover:bg-transparent">
              <TableHead className="pl-5">When</TableHead>
              <TableHead>Doctor</TableHead>
              <TableHead>Patient</TableHead>
              <TableHead>Reason</TableHead>
              <TableHead>Status</TableHead>
              <TableHead className="pr-5">Booked by</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {booked.map((appointment) => (
              <TableRow key={appointment.id}>
                <TableCell className="tabular pl-5">{clinicDateTime(appointment.starts_at, zone)}</TableCell>
                <TableCell>{appointment.doctor}</TableCell>
                <TableCell>
                  <span className="font-mono text-xs">{appointment.patient_id}</span>
                  {appointment.new_patient ? (
                    <StatusPill tone="accent" className="ml-2">
                      New patient
                    </StatusPill>
                  ) : null}
                </TableCell>
                <TableCell className="whitespace-normal">
                  {appointment.reason === null ? "–" : <MaskedText text={appointment.reason} />}
                </TableCell>
                <TableCell className="capitalize">{appointment.status}</TableCell>
                <TableCell className="pr-5">
                  {appointment.booked_by_call ? (
                    <span className="inline-flex items-center gap-1.5">
                      <Phone className="size-3.5 text-primary" aria-hidden />
                      Agent, call <CallLink callId={appointment.booked_by_call} />
                    </span>
                  ) : (
                    <span className="text-muted-foreground">Not by the agent</span>
                  )}
                </TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
        <ShowMore
          hasMore={appointments.hasNextPage}
          loading={appointments.isFetchingNextPage}
          onClick={() => void appointments.fetchNextPage()}
          label="Show older appointments"
          shown={`Showing ${plural(booked.length, "appointment")}`}
        />
      </>
    );
  }

  return (
    <>
      <PageHeader
        title="Appointments and slots"
        description="What is booked, and which times are still open for each doctor. This page only shows; nothing can be cancelled or moved from here."
        aside={`Week of ${ymdLabel(weekStart, { weekday: "long", month: "long", day: "numeric" })}`}
      />
      <div className="mb-6 grid grid-cols-2 gap-3 sm:gap-4 xl:grid-cols-4">
        {slots.isPending ? (
          [0, 1, 2, 3].map((index) => <BlockSkeleton key={index} className="h-28 w-full" label="Loading" />)
        ) : (
          <>
            <StatCard
              label="Booked this week"
              value={weekSlots.length - open.length}
              note={`Of ${plural(weekSlots.length, "slot")}`}
            />
            <StatCard
              label="Booked by the agent"
              value={inWeek.filter((each) => each.booked_by_call !== null).length}
              note={`${inWeek.filter((each) => each.booked_by_call !== null && each.new_patient).length} for a new patient`}
            />
            <StatCard label="Open slots" value={open.length} note={`Of ${weekSlots.length} this week`} />
            <StatCard
              label="Next open slot"
              value={
                next
                  ? `${ymdLabel(clinicYmd(next.starts_at, zone), { weekday: "short" })} ${clinicTime(next.starts_at, zone)}`
                  : "None this week"
              }
              note={
                next
                  ? `${next.doctor}, ${ymdLabel(clinicYmd(next.starts_at, zone), { month: "long", day: "numeric" })}`
                  : undefined
              }
            />
          </>
        )}
      </div>
      <div className="space-y-6">
        <Section
          title="Slots by doctor and day"
          description={`${range}. Each cell is one slot.`}
          action={
            <div className="flex gap-2">
              <Button variant="outline" onClick={() => setWeekStart(addDays(weekStart, -SLOT_DAYS))}>
                <ArrowLeft aria-hidden />
                Earlier week
              </Button>
              <Button variant="outline" onClick={() => setWeekStart(addDays(weekStart, SLOT_DAYS))}>
                Later week
                <ArrowRight aria-hidden />
              </Button>
            </div>
          }
        >
          <div className="border-b px-5 py-3">
            <Legend />
          </div>
          {grid}
        </Section>
        <Section
          title="Booked appointments"
          description="Newest booking first. Reasons are masked. Patients appear as ids."
        >
          {list}
        </Section>
      </div>
    </>
  );
}
