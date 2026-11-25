// Synthetic answers of the admin API for `npm run dev:mock` and the tests. Never imported by the
// app: the mock server (mock/server.ts) serves them over HTTP, so the app runs exactly as it does
// against the real API. Every value is made up; masked values are written as the API masks them.

import type {
  Appointment,
  AuditEvent,
  Call,
  CallDetail,
  Guardrails,
  LatencyRow,
  LiveCall,
  Metrics,
  QueueItem,
  Session,
  Slot,
  Turn,
  Verification,
} from "../lib/api/schemas";

export const CLINIC_TIME_ZONE = "America/New_York";

// A call id has the API's shape (a version 4 UUID without dashes).
export function fixtureCallId(prefix: string): string {
  return `${prefix}1d2a4c3e8f00a1b2c3d4e5f6`;
}

export function session(): Session {
  return {
    user: "admin",
    csrf_token: "csrf-fixture",
    expires_in: 1800,
    office_open: true,
    time_zone: CLINIC_TIME_ZONE,
  };
}

const timings = (vad: number, stt: number, first: number | null, audio: number, total: number) => ({
  vad_ms: vad,
  stt_ms: stt,
  first_token_ms: first,
  last_token_ms: first === null ? null : first + 420,
  first_audio_ms: audio,
  total_ms: total,
});

export function liveCalls(now: Date): LiveCall[] {
  const ago = (seconds: number) => new Date(now.getTime() - seconds * 1000).toISOString();
  return [
    {
      call_id: fixtureCallId("3b8f2d10"),
      started_at: ago(22),
      turn: 2,
      phase: "emergency",
      verified: false,
      patient_id: null,
      last_tool: null,
      ...timings(500, 74, null, 610, 640),
    },
    {
      call_id: fixtureCallId("f919d3c9"),
      started_at: ago(117),
      turn: 3,
      phase: "intake",
      verified: false,
      patient_id: null,
      last_tool: "verify_identity",
      ...timings(500, 88, 1270, 1810, 2310),
    },
    {
      call_id: fixtureCallId("9fbe75cb"),
      started_at: ago(244),
      turn: 6,
      phase: "serving",
      verified: true,
      patient_id: "P003",
      last_tool: "request_refill",
      ...timings(500, 81, 1510, 2060, 2560),
    },
  ];
}

type CallSeed = [string, string, number, Call["end_reason"], string | null, number, boolean];

// prefix, started (UTC), length in seconds, how it ended, patient, turns, office open
const CALL_SEEDS: CallSeed[] = [
  ["e3a94d15", "2026-11-25T15:27:04Z", 19, "caller_hung_up", null, 1, true],
  ["43cfad67", "2026-11-25T15:12:03Z", 161, "completed", "P001", 6, true],
  ["7c21b0aa", "2026-11-25T14:58:10Z", 38, "emergency", null, 2, true],
  ["a02b77d1", "2026-11-25T14:44:31Z", 192, "completed", null, 11, true],
  ["1d04e6f2", "2026-11-25T14:31:47Z", 115, "transferred", null, 7, true],
  ["5e7730c9", "2026-11-25T14:19:12Z", 124, "completed", "P002", 8, true],
  ["b8e19c40", "2026-11-25T02:04:55Z", 72, "completed", null, 5, false],
  ["2f6d81be", "2026-11-24T21:31:20Z", 97, "idle_timeout", "P004", 4, true],
  ["c47e09a3", "2026-11-24T19:15:02Z", 600, "max_duration", "P005", 19, true],
  ["0d9a51ee", "2026-11-24T16:02:41Z", 12, "error", null, 1, true],
  ["6a3e1f27", "2026-11-24T15:40:09Z", 141, "completed", "P002", 7, true],
  ["d51c88b4", "2026-11-24T14:12:30Z", 88, "transferred", "P001", 5, true],
];

export const CALLS: Call[] = CALL_SEEDS.map(
  ([prefix, started, seconds, reason, patient, turns, open], index) => ({
    id: 400 - index,
    call_id: fixtureCallId(prefix),
    started_at: new Date(started).toISOString().replace("Z", "+00:00"),
    ended_at: new Date(Date.parse(started) + seconds * 1000).toISOString().replace("Z", "+00:00"),
    office_open: open,
    verified: patient !== null,
    patient_id: patient,
    end_reason: reason,
    turn_count: turns,
  }),
);

const TRANSCRIPT = [
  "agent: Thank you for calling Sunrise Family Clinic. This call may be recorded for quality purposes. How can I help you today?",
  "caller: Hi, I'd like to check when my next appointment is.",
  "agent: I can help with that. First, please tell me your full name and your date of birth.",
  "caller: My full name is <PERSON> <PERSON>. And ****-**-14 is my date of birth.",
  "agent: Thank you. Now please say only the last four digits of your Social Security number.",
  "caller: (turn withheld)",
  "agent: Thank you, <PERSON>. You're verified. How can I help you today?",
  "caller: When is my next appointment?",
  "agent: Your next appointment is with Dr. Patel on Monday at 9:30 in the morning.",
  "caller: Great. Could you also ask for a refill of my lisinopril?",
  "agent: I've sent a refill request for lisinopril to your pharmacy on file. Staff will review it within one business day.",
  "caller: That's all, thank you.",
  "agent: You're welcome. Goodbye.",
].join("\n");

function at(base: string, seconds: number): string {
  return new Date(Date.parse(base) + seconds * 1000).toISOString().replace("Z", "+00:00");
}

function eventsOf(call: Call, firstSeq: number): AuditEvent[] {
  const steps: [number, string, string, string | null, AuditEvent["outcome"], string | null][] = [
    [0, "call_started", "call", call.call_id, "success", null],
  ];
  if (call.call_id === fixtureCallId("43cfad67")) {
    steps.push(
      [18, "identity_claimed", "call", call.call_id, "success", null],
      [31, "identity_verified", "patient", "P001", "success", null],
      [46, "appointment_read", "patient", "P001", "success", null],
      [77, "refill_requested", "patient", "P001", "success", null],
      [155, "call_closed", "call", call.call_id, "success", null],
    );
  } else if (call.end_reason === "emergency") {
    steps.push([30, "emergency_detected", "call", call.call_id, "success", "guard"]);
  } else if (call.end_reason === "transferred") {
    steps.push(
      [40, "identity_claimed", "call", call.call_id, "success", null],
      [72, "identity_locked", "call", call.call_id, "refused", null],
      [80, "transfer_requested", "call", call.call_id, "success", "verification_locked"],
    );
  }
  const length = (Date.parse(call.ended_at) - Date.parse(call.started_at)) / 1000;
  steps.push([length, "call_ended", "call", call.call_id, "success", call.end_reason]);
  return steps.map(([seconds, action, resourceType, resourceId, outcome, reason], index) => ({
    seq: firstSeq + index,
    ts: at(call.started_at, seconds),
    call_id: call.call_id,
    actor: "agent",
    action,
    resource_type: resourceType,
    resource_id: resourceId,
    outcome,
    reason,
  }));
}

export function callDetail(callId: string): CallDetail | null {
  const call = CALLS.find((each) => each.call_id === callId);
  if (call === undefined) {
    return null;
  }
  const withheld = call.call_id === fixtureCallId("b8e19c40");
  return {
    call: {
      ...call,
      transcript: withheld
        ? null
        : call.call_id === fixtureCallId("43cfad67")
          ? TRANSCRIPT
          : "agent: Thank you for calling Sunrise Family Clinic. How can I help you today?\ncaller: (turn withheld)",
      transcript_truncated: false,
      names_masked: !withheld,
      transcript_withheld: withheld ? "names_not_masked" : null,
    },
    events: eventsOf(call, 1268),
  };
}

export function turnsOf(callId: string): Turn[] {
  const call = CALLS.find((each) => each.call_id === callId);
  if (call === undefined) {
    return [];
  }
  const phases: Turn["phase"][] = ["intake", "intake", "serving", "serving", "serving", "closing"];
  const tools = [null, "verify_identity", "get_next_appointment", null, "request_refill", null];
  return Array.from({ length: Math.min(call.turn_count, 12) }, (_, index) => {
    const tool = tools[index % tools.length];
    const scripted = index === 2 && call.turn_count > 3;
    const first = scripted ? null : 980 + ((index * 137) % 700);
    return {
      turn: index + 1,
      at: at(call.started_at, 12 + index * 24),
      phase: phases[index % phases.length],
      verified: call.verified && index >= 2,
      patient_id: call.verified && index >= 2 ? call.patient_id : null,
      guard_action: index === 2 && call.turn_count > 3 ? "swallow" : "pass",
      last_tool: tool,
      tool_rounds: tool === null ? 0 : 1,
      vad_ms: 500,
      stt_ms: 70 + ((index * 13) % 40),
      first_token_ms: first,
      last_token_ms: first === null ? null : first + 380,
      first_audio_ms: first === null ? 560 : first + 520,
      total_ms: first === null ? 600 : first + 900,
      record_us: 140 + ((index * 31) % 90),
    };
  });
}

export const QUEUE: QueueItem[] = [
  {
    id: 96,
    created_at: "2026-11-25T15:45:12+00:00",
    call_id: fixtureCallId("9fbe75cb"),
    type: "refill",
    summary: "Metformin 500 mg at Walgreens Oak Ave",
    patient_id: "P003",
    callbacks: [],
    after_hours: false,
  },
  {
    id: 95,
    created_at: "2026-11-25T15:13:20+00:00",
    call_id: fixtureCallId("43cfad67"),
    type: "refill",
    summary: "Lisinopril 10 mg at CVS Main St",
    patient_id: "P001",
    callbacks: [],
    after_hours: false,
  },
  {
    id: 94,
    created_at: "2026-11-25T14:46:02+00:00",
    call_id: fixtureCallId("a02b77d1"),
    type: "booking",
    summary: "Dr. Patel, Monday 9:30 AM. New patient, confirm by email.",
    patient_id: "P006",
    callbacks: [],
    after_hours: false,
  },
  {
    id: 93,
    created_at: "2026-11-25T14:33:07+00:00",
    call_id: fixtureCallId("1d04e6f2"),
    type: "transfer",
    summary: "Caller passed to staff. Reason: verification_locked",
    patient_id: null,
    callbacks: [],
    after_hours: false,
  },
  {
    id: 92,
    created_at: "2026-11-25T14:20:41+00:00",
    call_id: fixtureCallId("5e7730c9"),
    type: "doctor_message",
    summary: "For Dr. Lee: should I keep taking this medication with food?",
    patient_id: "P002",
    callbacks: [],
    after_hours: false,
  },
  {
    id: 91,
    created_at: "2026-11-25T02:05:44+00:00",
    call_id: fixtureCallId("b8e19c40"),
    type: "callback",
    summary: "Needs to reschedule a visit.",
    patient_id: null,
    callbacks: ["***-***-3344"],
    after_hours: true,
  },
];

export const APPOINTMENTS: Appointment[] = [
  ["2026-11-30T09:30:00-05:00", "Dr. Patel", "P006", true, "New patient visit", fixtureCallId("a02b77d1")],
  ["2026-12-03T10:30:00-05:00", "Dr. Patel", "P001", false, "Follow-up", null],
  ["2026-12-04T15:30:00-05:00", "Dr. Patel", "P006", false, "Lab review", null],
  ["2026-12-01T14:00:00-05:00", "Dr. Lee", "P002", false, "Annual physical", fixtureCallId("5e7730c9")],
  ["2026-12-02T09:30:00-05:00", "Dr. Lee", "P004", false, "Blood pressure check", null],
  ["2026-11-24T10:30:00-05:00", "Dr. Lee", "P005", false, "Follow-up", null],
].map(([startsAt, doctor, patient, isNew, reason, call], index) => ({
  id: 60 - index,
  starts_at: startsAt as string,
  starts_at_utc: new Date(startsAt as string).toISOString().replace("Z", "+00:00"),
  doctor: doctor as string,
  patient_id: patient as string,
  new_patient: isNew as boolean,
  reason: reason as string,
  status: "scheduled",
  booked_by_call: call as string | null,
}));

const SLOT_TIMES = ["09:30", "10:30", "14:00", "15:30"];

// The slots of `days` clinic days from `from` (YYYY-MM-DD): weekdays only, Dr. Lee not on Fridays,
// taken where an appointment is.
export function slots(from: string, days: number): Slot[] {
  const result: Slot[] = [];
  const start = Date.parse(`${from}T12:00:00Z`);
  for (let day = 0; day < days; day += 1) {
    const date = new Date(start + day * 86_400_000);
    const weekday = date.getUTCDay();
    if (weekday === 0 || weekday === 6) {
      continue;
    }
    const ymd = date.toISOString().slice(0, 10);
    for (const doctor of ["Dr. Patel", "Dr. Lee"]) {
      if (doctor === "Dr. Lee" && weekday === 5) {
        continue;
      }
      for (const time of SLOT_TIMES) {
        const startsAt = `${ymd}T${time}:00-05:00`;
        result.push({
          id: result.length + 1,
          doctor,
          starts_at: startsAt,
          available: !APPOINTMENTS.some(
            (appointment) => appointment.doctor === doctor && appointment.starts_at === startsAt,
          ),
        });
      }
    }
  }
  return result;
}

const AUDIT_ACTIONS: [
  string,
  string,
  string,
  string | null,
  AuditEvent["outcome"],
  string | null,
  string | null,
][] = [
  ["admin:admin", "admin_read_audit", "audit_event", null, "success", null, null],
  ["agent", "refill_requested", "patient", "P003", "success", null, "9fbe75cb"],
  ["agent", "identity_claimed", "call", null, "success", null, "f919d3c9"],
  ["agent", "tool_refused", "tool", "get_next_appointment", "refused", null, "f919d3c9"],
  ["agent", "call_started", "call", null, "success", null, "f919d3c9"],
  ["agent", "identity_verified", "patient", "P003", "success", null, "9fbe75cb"],
  ["agent", "identity_claimed", "call", null, "success", null, "9fbe75cb"],
  ["agent", "call_started", "call", null, "success", null, "9fbe75cb"],
  ["admin:admin", "admin_read_calls", "call", null, "success", null, null],
  ["agent", "call_ended", "call", null, "success", "caller_hung_up", "e3a94d15"],
  ["agent", "guard_block", "call", null, "refused", "medical_advice", "e3a94d15"],
  ["agent", "call_started", "call", null, "success", null, "e3a94d15"],
  ["agent", "call_ended", "call", null, "success", "completed", "43cfad67"],
  ["agent", "call_closed", "call", null, "success", null, "43cfad67"],
  ["agent", "refill_requested", "patient", "P001", "success", null, "43cfad67"],
  ["agent", "appointment_read", "patient", "P001", "success", null, "43cfad67"],
  ["agent", "identity_verified", "patient", "P001", "success", null, "43cfad67"],
  ["agent", "identity_claimed", "call", null, "success", null, "43cfad67"],
  ["agent", "call_started", "call", null, "success", null, "43cfad67"],
  ["agent", "call_ended", "call", null, "success", "emergency", "7c21b0aa"],
  ["agent", "emergency_detected", "call", null, "success", "guard", "7c21b0aa"],
  ["agent", "call_started", "call", null, "success", null, "7c21b0aa"],
];

export const AUDIT: AuditEvent[] = AUDIT_ACTIONS.map(
  ([actor, action, resourceType, resourceId, outcome, reason, call], index) => {
    const callId = call === null ? null : fixtureCallId(call);
    return {
      seq: 1285 - index,
      ts: new Date(Date.parse("2026-11-25T15:46:12Z") - index * 47_000).toISOString().replace("Z", "+00:00"),
      call_id: callId,
      actor,
      action,
      resource_type: resourceType,
      resource_id: resourceId ?? (resourceType === "call" ? callId : null),
      outcome,
      reason,
    };
  },
);

export const VERIFICATION: Verification = { ok: true, checked: 1285, breaks: [], truncated: false };

const WEEK = [
  "2026-11-19",
  "2026-11-20",
  "2026-11-21",
  "2026-11-22",
  "2026-11-23",
  "2026-11-24",
  "2026-11-25",
];
const day = (ymd: string) => `${ymd}T00:00:00-05:00`;

export function guardrails(days: number): Guardrails {
  const window = WEEK.slice(-Math.min(days, WEEK.length));
  const counts: Guardrails["counts"] = [];
  const add = (index: number, action: string, reason: string | null, count: number) => {
    if (index >= WEEK.length - window.length) {
      counts.push({ day: day(WEEK[index]), action, reason, count });
    }
  };
  add(0, "guard_block", "medical_advice", 1);
  add(0, "guard_block", "refill_promise", 1);
  add(1, "guard_block", "medical_advice", 1);
  add(3, "guard_block", "ssn_last4", 1);
  add(4, "guard_block", "medical_advice", 1);
  add(4, "guard_block", "phi_outside_gate", 1);
  add(4, "tool_refused", null, 2);
  add(5, "guard_block", "medical_advice", 1);
  add(5, "guard_block", "refill_promise", 1);
  add(5, "emergency_detected", "model", 1);
  add(5, "identity_locked", null, 1);
  add(6, "guard_block", "medical_advice", 1);
  add(6, "emergency_detected", "guard", 2);
  add(6, "tool_refused", null, 2);
  add(6, "identity_refused", null, 3);
  add(6, "identity_locked", null, 1);
  return { days, since: day(window[0]), counts };
}

function latency(): LatencyRow[] {
  const figures: Record<
    LatencyRow["kind"],
    [number, Partial<Record<LatencyRow["stage"], [number, number]>>]
  > = {
    scripted: [12, {}],
    plain: [
      148,
      {
        vad_ms: [500, 500],
        stt_ms: [82, 161],
        first_token_ms: [1180, 1940],
        last_token_ms: [1610, 2750],
        first_audio_ms: [1790, 2620],
        total_ms: [2410, 3980],
      },
    ],
    tool: [
      71,
      {
        vad_ms: [500, 500],
        stt_ms: [85, 170],
        first_token_ms: [2240, 3460],
        last_token_ms: [2700, 4310],
        first_audio_ms: [2870, 4120],
        total_ms: [3620, 5890],
      },
    ],
  };
  return (["scripted", "plain", "tool"] as const).flatMap((kind) => {
    const [turns, stages] = figures[kind];
    return (
      ["vad_ms", "stt_ms", "first_token_ms", "last_token_ms", "first_audio_ms", "total_ms"] as const
    ).map((stage) => {
      const figure = stages[stage];
      const values =
        kind === "scripted" && (stage === "first_token_ms" || stage === "last_token_ms") ? 0 : turns;
      return {
        kind,
        turns,
        stage,
        values,
        p50: figure === undefined ? null : figure[0],
        p95: figure === undefined ? null : figure[1],
      };
    });
  });
}

export function metrics(days: number): Metrics {
  const perDay = [6, 8, 1, 2, 9, 7, 6];
  const window = perDay.slice(-Math.min(days, perDay.length));
  return {
    days,
    since: day(WEEK[WEEK.length - window.length]),
    calls_per_day: window.map((calls, index) => ({
      day: day(WEEK[WEEK.length - window.length + index]),
      calls,
    })),
    ended: [
      { reason: "caller_hung_up", calls: 3 },
      { reason: "completed", calls: 27 },
      { reason: "emergency", calls: 3 },
      { reason: "error", calls: 0 },
      { reason: "idle_timeout", calls: 1 },
      { reason: "transferred", calls: 5 },
    ].filter((row) => row.calls > 0),
    verified_calls: 20,
    verification_attempts: 26,
    verification_successes: 20,
    latency: latency(),
    record_us_turns: 231,
    record_us_p50: 182,
    turn_rows_dropped: 0,
  };
}
