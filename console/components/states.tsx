import { CircleAlert, RotateCw, type LucideIcon } from "lucide-react";
import type { ReactNode } from "react";

import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { ApiError } from "@/lib/api/client";

// The loading, empty and error states every page uses.

export function TableSkeleton({ rows = 6, label }: { rows?: number; label: string }) {
  return (
    <div role="status" aria-label={label} className="space-y-3 px-5 py-5">
      {Array.from({ length: rows }, (_, index) => (
        <div key={index} className="flex items-center gap-4">
          <Skeleton className="h-4 w-20" />
          <Skeleton className="h-4 flex-1" />
          <Skeleton className="hidden h-4 w-24 sm:block" />
          <Skeleton className="hidden h-4 w-16 md:block" />
        </div>
      ))}
    </div>
  );
}

export function BlockSkeleton({ className, label }: { className?: string; label: string }) {
  return (
    <div role="status" aria-label={label}>
      <Skeleton className={className ?? "h-48 w-full"} />
    </div>
  );
}

export function EmptyState({
  icon: Icon,
  title,
  children,
}: {
  icon: LucideIcon;
  title: string;
  children?: ReactNode;
}) {
  return (
    <div className="flex flex-col items-center px-6 py-12 text-center">
      <span className="mb-3 flex size-10 items-center justify-center rounded-full bg-secondary text-secondary-foreground">
        <Icon className="size-5" aria-hidden />
      </span>
      <p className="font-medium">{title}</p>
      {children ? <div className="mt-1 max-w-md text-sm text-muted-foreground">{children}</div> : null}
    </div>
  );
}

function explain(error: unknown): { title: string; body: string } {
  const kind = error instanceof ApiError ? error.kind : "network";
  switch (kind) {
    case "invalid_answer":
      return {
        title: "This page could not be shown",
        body: "The admin API answered with something the console does not recognise, so nothing of it is shown.",
      };
    case "not_found":
      return { title: "Not found", body: "There is nothing at this address." };
    case "rate_limited":
      return { title: "Too many requests", body: "Wait a moment, then try again." };
    case "timeout":
      return { title: "The admin API took too long", body: "Nothing was shown. Try again." };
    case "unavailable":
      return {
        title: "The admin API is unavailable",
        body: "It could not read this right now. Nothing was shown. Try again in a moment.",
      };
    case "rejected":
      return { title: "The admin API refused this request", body: "Nothing was shown." };
    default:
      return {
        title: "The admin API could not be reached",
        body: "Check the connection, then try again.",
      };
  }
}

export function ErrorState({ error, onRetry }: { error: unknown; onRetry?: () => void }) {
  const { title, body } = explain(error);
  return (
    <div role="alert" className="flex flex-col items-center px-6 py-12 text-center">
      <span className="mb-3 flex size-10 items-center justify-center rounded-full bg-destructive/10 text-destructive">
        <CircleAlert className="size-5" aria-hidden />
      </span>
      <p className="font-medium">{title}</p>
      <p className="mt-1 max-w-md text-sm text-muted-foreground">{body}</p>
      {onRetry ? (
        <Button variant="outline" className="mt-4" onClick={onRetry}>
          <RotateCw aria-hidden />
          Try again
        </Button>
      ) : null}
    </div>
  );
}

export function ShowMore({
  hasMore,
  loading,
  onClick,
  label,
  shown,
}: {
  hasMore: boolean;
  loading: boolean;
  onClick: () => void;
  label: string;
  shown: string;
}) {
  return (
    <div className="flex flex-wrap items-center justify-between gap-3 border-t px-5 py-3 text-sm text-muted-foreground">
      <span>{shown}</span>
      {hasMore ? (
        <Button variant="outline" onClick={onClick} disabled={loading}>
          {loading ? "Loading…" : label}
        </Button>
      ) : (
        <span>That is all of them.</span>
      )}
    </div>
  );
}
