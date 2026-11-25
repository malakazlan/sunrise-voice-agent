import type { ReactNode } from "react";

import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { cn } from "@/lib/utils";

export function PageHeader({
  title,
  description,
  aside,
}: {
  title: string;
  description: ReactNode;
  aside?: ReactNode;
}) {
  return (
    <header className="mb-6 flex flex-col gap-3 md:flex-row md:items-end md:justify-between">
      <div className="max-w-2xl">
        <h1 className="font-heading text-2xl font-semibold tracking-tight text-balance sm:text-[1.75rem]">
          {title}
        </h1>
        <p className="mt-1.5 text-[0.95rem] text-pretty text-muted-foreground">{description}</p>
      </div>
      {aside ? <div className="text-sm text-muted-foreground md:max-w-sm md:text-right">{aside}</div> : null}
    </header>
  );
}

// A card with a title, a line on what it shows, an optional action, and its content. Tables put
// inside it scroll sideways within it, never the page.
export function Section({
  title,
  description,
  action,
  children,
  className,
  flush = true,
}: {
  title: string;
  description?: ReactNode;
  action?: ReactNode;
  children: ReactNode;
  className?: string;
  flush?: boolean;
}) {
  return (
    <Card className={cn("gap-0 py-0", className)}>
      <CardHeader className="gap-1 border-b px-5 py-4 sm:flex sm:flex-wrap sm:items-start sm:justify-between">
        <div className="min-w-0">
          <CardTitle className="font-heading text-base font-semibold">
            <h2>{title}</h2>
          </CardTitle>
          {description ? <CardDescription className="mt-1">{description}</CardDescription> : null}
        </div>
        {action ? <div className="mt-3 sm:mt-0">{action}</div> : null}
      </CardHeader>
      <CardContent className={cn(flush ? "px-0" : "px-5 py-5")}>{children}</CardContent>
    </Card>
  );
}

export function StatCard({
  label,
  value,
  note,
  tone = "default",
  icon,
}: {
  label: string;
  value: ReactNode;
  note?: ReactNode;
  tone?: "default" | "emergency";
  icon?: ReactNode;
}) {
  return (
    <Card
      className={cn(
        "gap-1 px-4 py-4 sm:px-5",
        tone === "emergency" && "bg-emergency-soft/60 ring-emergency/40",
      )}
    >
      <p className="flex items-center gap-1.5 text-sm text-muted-foreground">
        {icon}
        {label}
      </p>
      <p className="font-heading text-xl font-semibold tracking-tight text-balance sm:text-2xl">{value}</p>
      {note ? <p className="text-xs text-muted-foreground">{note}</p> : null}
    </Card>
  );
}
