import {
  ArrowUpRight,
  Check,
  CircleAlert,
  CircleDashed,
  CircleMinus,
  Clock,
  Hourglass,
  Lock,
  Moon,
  PhoneOff,
  TriangleAlert,
  X,
  type LucideIcon,
} from "lucide-react";
import type { ReactNode } from "react";

import { cn } from "@/lib/utils";
import type { EndReason, Phase } from "@/lib/api/schemas";
import { END_REASON_LABEL, PHASE_LABEL } from "@/lib/labels";

// Every status is a word with an icon; colour only repeats what the word says. Orange means an
// emergency and nothing else; red is only for failures.

type Tone = "neutral" | "good" | "emergency" | "failure" | "accent";

const TONE: Record<Tone, string> = {
  neutral: "bg-muted text-foreground ring-border",
  good: "bg-success-soft text-success ring-success/20",
  emergency: "bg-emergency-soft text-emergency ring-emergency/30",
  failure: "bg-destructive/8 text-destructive ring-destructive/20",
  accent: "bg-secondary text-secondary-foreground ring-primary/15",
};

export function StatusPill({
  tone,
  icon: Icon,
  children,
  className,
}: {
  tone: Tone;
  icon?: LucideIcon;
  children: ReactNode;
  className?: string;
}) {
  return (
    <span
      className={cn(
        "inline-flex items-center gap-1 rounded-full px-2 py-0.5 text-xs font-medium whitespace-nowrap ring-1 ring-inset",
        TONE[tone],
        className,
      )}
    >
      {Icon ? <Icon className="size-3.5 shrink-0" aria-hidden /> : null}
      {children}
    </span>
  );
}

export function PhaseBadge({ phase }: { phase: Phase | null }) {
  if (phase === null) {
    return (
      <StatusPill tone="neutral" icon={CircleDashed}>
        No turn yet
      </StatusPill>
    );
  }
  if (phase === "emergency") {
    return (
      <StatusPill tone="emergency" icon={TriangleAlert}>
        {PHASE_LABEL[phase]}
      </StatusPill>
    );
  }
  return <StatusPill tone="accent">{PHASE_LABEL[phase]}</StatusPill>;
}

const END_REASON_STYLE: Record<EndReason, [Tone, LucideIcon]> = {
  completed: ["good", Check],
  emergency: ["emergency", TriangleAlert],
  transferred: ["neutral", ArrowUpRight],
  caller_hung_up: ["neutral", PhoneOff],
  idle_timeout: ["neutral", Clock],
  max_duration: ["neutral", Hourglass],
  error: ["failure", CircleAlert],
};

export function EndReasonBadge({ reason }: { reason: EndReason }) {
  const [tone, icon] = END_REASON_STYLE[reason];
  return (
    <StatusPill tone={tone} icon={icon}>
      {END_REASON_LABEL[reason]}
    </StatusPill>
  );
}

export function CallerBadge({
  verified,
  patientId,
  officeOpen = true,
}: {
  verified: boolean | null;
  patientId: string | null;
  officeOpen?: boolean;
}) {
  if (verified && patientId !== null) {
    return (
      <span className="inline-flex items-center gap-1.5">
        <StatusPill tone="good" icon={Check}>
          Verified
        </StatusPill>
        <span className="font-mono text-xs">{patientId}</span>
      </span>
    );
  }
  if (!officeOpen) {
    return (
      <StatusPill tone="neutral" icon={Moon}>
        After hours
      </StatusPill>
    );
  }
  return (
    <StatusPill tone="neutral" icon={CircleMinus}>
      Not verified
    </StatusPill>
  );
}

export function OutcomeBadge({ outcome }: { outcome: "success" | "failure" | "refused" }) {
  if (outcome === "success") {
    return (
      <StatusPill tone="good" icon={Check}>
        success
      </StatusPill>
    );
  }
  if (outcome === "refused") {
    return (
      <StatusPill tone="neutral" icon={Lock}>
        refused
      </StatusPill>
    );
  }
  return (
    <StatusPill tone="failure" icon={X}>
      failure
    </StatusPill>
  );
}
