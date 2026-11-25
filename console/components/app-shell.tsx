"use client";

import {
  CalendarDays,
  ChartColumn,
  CircleCheck,
  CircleQuestionMark,
  History,
  Inbox,
  LogOut,
  Menu,
  Moon,
  Phone,
  PhoneOff,
  ShieldCheck,
  ShieldHalf,
  SlidersHorizontal,
  Sunrise,
  UserRound,
  type LucideIcon,
} from "lucide-react";
import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { useState, type ReactNode } from "react";

import { ClinicContext } from "@/components/clinic";
import { ErrorState } from "@/components/states";
import { Button } from "@/components/ui/button";
import { Sheet, SheetContent, SheetDescription, SheetTitle, SheetTrigger } from "@/components/ui/sheet";
import { Skeleton } from "@/components/ui/skeleton";
import { useSession, useSignOut } from "@/lib/api/hooks";
import type { Session } from "@/lib/api/schemas";
import { zoneName } from "@/lib/format";
import { cn } from "@/lib/utils";

interface NavItem {
  href: string;
  label: string;
  icon: LucideIcon;
}

const NAV: { group: string; items: NavItem[] }[] = [
  {
    group: "Calls",
    items: [
      { href: "/", label: "Live calls", icon: Phone },
      { href: "/calls", label: "Call history", icon: History },
    ],
  },
  {
    group: "Work for staff",
    items: [
      { href: "/requests", label: "Requests", icon: Inbox },
      { href: "/appointments", label: "Appointments", icon: CalendarDays },
    ],
  },
  {
    group: "Oversight",
    items: [
      { href: "/audit", label: "Audit log", icon: ShieldCheck },
      { href: "/guardrails", label: "Guardrails", icon: ShieldHalf },
      { href: "/metrics", label: "Metrics", icon: ChartColumn },
    ],
  },
  {
    group: "Settings",
    items: [{ href: "/config", label: "Config", icon: SlidersHorizontal }],
  },
];

function isCurrent(pathname: string, href: string): boolean {
  return href === "/" ? pathname === "/" : pathname === href || pathname.startsWith(`${href}/`);
}

function Brand() {
  return (
    <div className="flex items-center gap-3 px-2">
      <span className="flex size-9 items-center justify-center rounded-lg bg-sidebar-accent text-sidebar-primary">
        <Sunrise className="size-5" aria-hidden />
      </span>
      <div className="leading-tight">
        <p className="font-heading text-[0.95rem] font-semibold text-white">Sunrise Family Clinic</p>
        <p className="text-xs text-sidebar-muted">Voice agent console</p>
      </div>
    </div>
  );
}

function Navigation({ onNavigate }: { onNavigate?: () => void }) {
  const pathname = usePathname();
  return (
    <nav aria-label="Console" className="flex flex-col gap-5">
      {NAV.map(({ group, items }) => (
        <div key={group}>
          <p className="mb-1.5 px-3 text-[0.7rem] font-semibold tracking-[0.08em] text-sidebar-muted uppercase">
            {group}
          </p>
          <ul className="flex flex-col gap-0.5">
            {items.map(({ href, label, icon: Icon }) => {
              const current = isCurrent(pathname, href);
              return (
                <li key={href}>
                  <Link
                    href={href}
                    onClick={onNavigate}
                    aria-current={current ? "page" : undefined}
                    className={cn(
                      "flex min-h-10 items-center gap-3 rounded-lg px-3 text-[0.925rem] text-sidebar-foreground transition-colors hover:bg-sidebar-accent/70 focus-visible:outline-sidebar-ring",
                      current &&
                        "bg-sidebar-accent font-medium text-white shadow-[inset_3px_0_0_var(--sidebar-primary)]",
                    )}
                  >
                    <Icon className="size-[1.1rem] shrink-0" aria-hidden />
                    <span className="flex-1">{label}</span>
                  </Link>
                </li>
              );
            })}
          </ul>
        </div>
      ))}
    </nav>
  );
}

function AuditNote() {
  return (
    <p className="flex gap-2 px-2 text-xs leading-relaxed text-sidebar-muted">
      <ShieldCheck className="mt-0.5 size-3.5 shrink-0" aria-hidden />
      Page views and actions are recorded in the audit log.
    </p>
  );
}

function TopBar({ menu, right, banner }: { menu: ReactNode; right: ReactNode; banner: ReactNode }) {
  return (
    <header className="sticky top-0 z-30 border-b bg-card/95 backdrop-blur supports-[backdrop-filter]:bg-card/80">
      <div className="flex min-h-16 items-center gap-3 px-4 sm:px-6 lg:px-8">
        {menu}
        {right}
      </div>
      {banner}
    </header>
  );
}

// On every page while the phone assistant is switched off, or while its settings could not be
// read. The session says which; the shell reads it again every 60 s while the tab is shown, so the
// banner follows a change someone else made, and no page asks for the config itself.
export function SettingsBanner({ calls }: { calls: Session["calls"] }) {
  if (calls === "on") return null;
  const off = calls === "off";
  return (
    <div
      role="status"
      className={cn(
        "flex items-center gap-2 border-t px-4 py-2 text-sm font-medium sm:px-6 lg:px-8",
        off
          ? "border-destructive/25 bg-destructive/8 text-destructive"
          : "border-border bg-muted text-muted-foreground",
      )}
    >
      {off ? (
        <PhoneOff className="size-4 shrink-0" aria-hidden />
      ) : (
        <CircleQuestionMark className="size-4 shrink-0" aria-hidden />
      )}
      <span className="flex-1">
        {off
          ? "The phone assistant is switched off. New calls are refused."
          : "The assistant's settings could not be read just now."}
      </span>
      <Link href="/config" className="shrink-0 underline underline-offset-3">
        Config
      </Link>
    </div>
  );
}

function SignedInBar() {
  const session = useSession();
  const signOut = useSignOut();
  const router = useRouter();
  if (session.data === undefined) {
    return (
      <div className="flex flex-1 items-center justify-end gap-3">
        <Skeleton className="h-5 w-40" />
      </div>
    );
  }
  const { office_open: officeOpen, user, time_zone: zone } = session.data;
  return (
    <div className="flex min-w-0 flex-1 flex-wrap items-center justify-between gap-x-6 gap-y-1 py-2">
      <div className="flex min-w-0 flex-wrap items-center gap-x-4 gap-y-1 text-sm">
        <span className="inline-flex items-center gap-1.5 font-medium">
          {officeOpen ? (
            <CircleCheck className="size-4 text-success" aria-hidden />
          ) : (
            <Moon className="size-4 text-muted-foreground" aria-hidden />
          )}
          Office {officeOpen ? "open" : "closed"}
        </span>
        <span className="hidden text-muted-foreground sm:inline">
          Times in {zoneName(zone)} ({zone})
        </span>
      </div>
      <div className="flex items-center gap-3 text-sm">
        <span className="hidden items-center gap-1.5 text-muted-foreground md:inline-flex">
          <UserRound className="size-4" aria-hidden />
          Signed in as <span className="font-medium text-foreground">{user}</span>
        </span>
        <Button
          variant="ghost"
          className="text-primary"
          disabled={signOut.isPending}
          onClick={() =>
            // The API ends the session whatever it answers: every outcome ends at sign-in.
            signOut.mutate(undefined, { onSettled: () => router.replace("/login") })
          }
        >
          <LogOut aria-hidden />
          Sign out
        </Button>
      </div>
    </div>
  );
}

export function AppShell({ children }: { children: ReactNode }) {
  const session = useSession();
  const [menuOpen, setMenuOpen] = useState(false);

  let content: ReactNode;
  if (session.data !== undefined) {
    content = <ClinicContext.Provider value={session.data}>{children}</ClinicContext.Provider>;
  } else if (session.isError) {
    content = <ErrorState error={session.error} onRetry={() => void session.refetch()} />;
  } else {
    content = (
      <div role="status" aria-label="Loading the console" className="space-y-4">
        <Skeleton className="h-9 w-64" />
        <Skeleton className="h-5 w-96 max-w-full" />
        <Skeleton className="mt-6 h-64 w-full" />
      </div>
    );
  }

  return (
    <div className="flex min-h-dvh">
      <aside className="sticky top-0 hidden h-dvh w-64 shrink-0 flex-col gap-6 overflow-y-auto bg-sidebar px-3 py-5 lg:flex">
        <Brand />
        <div className="flex-1">
          <Navigation />
        </div>
        <div className="border-t border-sidebar-border pt-4">
          <AuditNote />
        </div>
      </aside>
      <div className="flex min-w-0 flex-1 flex-col">
        <TopBar
          menu={
            <Sheet open={menuOpen} onOpenChange={setMenuOpen}>
              <SheetTrigger asChild>
                <Button variant="outline" size="icon-lg" className="lg:hidden" aria-label="Open the menu">
                  <Menu aria-hidden />
                </Button>
              </SheetTrigger>
              <SheetContent
                side="left"
                className="w-72 gap-6 border-sidebar-border bg-sidebar px-3 py-5 text-sidebar-foreground"
              >
                <SheetTitle className="sr-only">Menu</SheetTitle>
                <SheetDescription className="sr-only">The console&apos;s pages</SheetDescription>
                <Brand />
                <Navigation onNavigate={() => setMenuOpen(false)} />
                <div className="mt-auto border-t border-sidebar-border pt-4">
                  <AuditNote />
                </div>
              </SheetContent>
            </Sheet>
          }
          right={<SignedInBar />}
          banner={session.data === undefined ? null : <SettingsBanner calls={session.data.calls} />}
        />
        <main id="main" className="w-full max-w-[1400px] flex-1 px-4 py-6 sm:px-6 lg:px-8 lg:py-8">
          {content}
        </main>
      </div>
    </div>
  );
}

export function LivePulse({ active }: { active: boolean }) {
  return (
    <span className="relative inline-flex size-2.5" aria-hidden>
      {active ? (
        <span className="absolute inline-flex size-full animate-ping rounded-full bg-success/60 motion-reduce:hidden" />
      ) : null}
      <span
        className={cn(
          "relative inline-flex size-2.5 rounded-full",
          active ? "bg-success" : "bg-muted-foreground",
        )}
      />
    </span>
  );
}
