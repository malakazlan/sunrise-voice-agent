// Times are shown in the clinic's time zone, which the API names (GET /admin/api/session); the
// browser's own zone is never used. Formatters are made once per zone and pattern.

const formatters = new Map<string, Intl.DateTimeFormat>();

function formatter(zone: string, options: Intl.DateTimeFormatOptions): Intl.DateTimeFormat {
  const key = `${zone}|${JSON.stringify(options)}`;
  let made = formatters.get(key);
  if (made === undefined) {
    made = new Intl.DateTimeFormat("en-US", { timeZone: zone, ...options });
    formatters.set(key, made);
  }
  return made;
}

export function clinicTime(iso: string, zone: string): string {
  return formatter(zone, { hour: "numeric", minute: "2-digit" }).format(new Date(iso));
}

export function clinicTimeSeconds(value: string | Date, zone: string): string {
  return formatter(zone, { hour: "numeric", minute: "2-digit", second: "2-digit" }).format(new Date(value));
}

export function clinicDateTime(iso: string, zone: string): string {
  return formatter(zone, { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" }).format(
    new Date(iso),
  );
}

export function clinicLongDate(iso: string, zone: string): string {
  return formatter(zone, { weekday: "long", month: "long", day: "numeric", year: "numeric" }).format(
    new Date(iso),
  );
}

export function clinicShortDate(iso: string, zone: string): string {
  return formatter(zone, { month: "short", day: "numeric" }).format(new Date(iso));
}

export function clinicWeekday(iso: string, zone: string): string {
  return formatter(zone, { weekday: "short" }).format(new Date(iso));
}

// The clinic's calendar day of an instant, as YYYY-MM-DD.
export function clinicYmd(value: string | Date, zone: string): string {
  const parts = formatter(zone, { year: "numeric", month: "2-digit", day: "2-digit" }).formatToParts(
    new Date(value),
  );
  const part = (type: string) => parts.find((each) => each.type === type)?.value ?? "";
  return `${part("year")}-${part("month")}-${part("day")}`;
}

export function zoneName(zone: string): string {
  const parts = formatter(zone, { timeZoneName: "longGeneric" }).formatToParts(new Date());
  return parts.find((part) => part.type === "timeZoneName")?.value ?? zone;
}

// Calendar arithmetic on YYYY-MM-DD days, independent of any time zone.
export function addDays(ymd: string, days: number): string {
  const date = new Date(`${ymd}T00:00:00Z`);
  date.setUTCDate(date.getUTCDate() + days);
  return date.toISOString().slice(0, 10);
}

export function mondayOf(ymd: string): string {
  const weekday = new Date(`${ymd}T00:00:00Z`).getUTCDay();
  return addDays(ymd, weekday === 0 ? -6 : 1 - weekday);
}

export function ymdLabel(ymd: string, options: Intl.DateTimeFormatOptions): string {
  return formatter("UTC", options).format(new Date(`${ymd}T12:00:00Z`));
}

export function duration(totalSeconds: number): string {
  const seconds = Math.max(0, Math.round(totalSeconds));
  const minutes = Math.floor(seconds / 60);
  const rest = String(seconds % 60).padStart(2, "0");
  return minutes === 0 ? `${seconds} s` : `${minutes} min ${rest} s`;
}

export function secondsBetween(from: string, to: string | Date): number {
  return (new Date(to).getTime() - new Date(from).getTime()) / 1000;
}

// A stage's time in seconds to two places, bare (for a column headed "in seconds") or with its unit.
export function secondsFigure(milliseconds: number): string {
  return (milliseconds / 1000).toFixed(2);
}

export function stageSeconds(milliseconds: number): string {
  return `${secondsFigure(milliseconds)} s`;
}

export function shortId(callId: string): string {
  return callId.slice(0, 8);
}

export function percent(part: number, whole: number): string {
  return whole === 0 ? "–" : `${Math.round((part / whole) * 100)}%`;
}

export function plural(count: number, one: string, many = `${one}s`): string {
  return `${count.toLocaleString("en-US")} ${count === 1 ? one : many}`;
}
