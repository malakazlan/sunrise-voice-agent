"use client";

import { CircleAlert, Clock, EyeOff, Lock, ShieldCheck, Sunrise } from "lucide-react";
import { useRouter } from "next/navigation";
import { useState, type FormEvent } from "react";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { ApiError } from "@/lib/api/client";
import { useSignIn } from "@/lib/api/hooks";

// The same words for every refused sign-in, whichever half was wrong.
function failureOf(error: unknown): { limited: boolean; text: string } {
  if (error instanceof ApiError && error.kind === "rate_limited") {
    const wait = error.retryAfterSeconds;
    return {
      limited: true,
      text: `Too many attempts. Wait ${wait === null || wait === 60 ? "one minute" : `${wait} seconds`}, then try again.`,
    };
  }
  if (error instanceof ApiError && ["network", "timeout", "unavailable"].includes(error.kind)) {
    return { limited: false, text: "The admin API could not be reached. Try again in a moment." };
  }
  return { limited: false, text: "That user name or password is not right. Check both and try again." };
}

export function SignIn() {
  const router = useRouter();
  const signIn = useSignIn();
  const [user, setUser] = useState("");
  const [password, setPassword] = useState("");

  const submit = (event: FormEvent) => {
    event.preventDefault();
    signIn.mutate(
      { user, password },
      {
        onSuccess: () => router.replace("/"),
        onError: () => setPassword(""),
      },
    );
  };

  const failure = signIn.isError ? failureOf(signIn.error) : null;

  return (
    <div className="grid min-h-dvh lg:grid-cols-[minmax(0,1fr)_minmax(0,1fr)]">
      <section className="flex flex-col justify-between gap-10 bg-sidebar px-6 py-8 text-sidebar-foreground sm:px-10 lg:py-12">
        <div className="flex items-center gap-3">
          <span className="flex size-9 items-center justify-center rounded-lg bg-sidebar-accent text-sidebar-primary">
            <Sunrise className="size-5" aria-hidden />
          </span>
          <div className="leading-tight">
            <p className="font-heading text-[0.95rem] font-semibold text-white">Sunrise Family Clinic</p>
            <p className="text-xs text-sidebar-muted">Voice agent console</p>
          </div>
        </div>
        <div className="max-w-md">
          <p className="font-heading text-3xl leading-tight font-semibold text-balance text-white sm:text-4xl">
            See what the phone agent did on every call.
          </p>
          <p className="mt-4 text-sidebar-muted">
            For clinic staff and reviewers: calls as they happen, what staff need to act on, and proof that
            every look at patient data was recorded.
          </p>
        </div>
        <ul className="hidden max-w-lg space-y-4 text-sm sm:block">
          <li className="flex gap-3">
            <EyeOff className="mt-0.5 size-4 shrink-0 text-sidebar-primary" aria-hidden />
            <span>
              <span className="block font-medium text-white">Everything shown is masked</span>
              No names, dates of birth, full phone numbers or SSN digits appear anywhere in the console.
            </span>
          </li>
          <li className="flex gap-3">
            <ShieldCheck className="mt-0.5 size-4 shrink-0 text-sidebar-primary" aria-hidden />
            <span>
              <span className="block font-medium text-white">Everything you do is recorded</span>
              Each page you open is written to the audit log under your sign-in name.
            </span>
          </li>
        </ul>
      </section>

      <main className="flex items-center justify-center px-6 py-10 sm:px-10">
        <div className="w-full max-w-sm">
          <h1 className="font-heading text-2xl font-semibold tracking-tight">Sign in</h1>
          <p className="mt-1 text-muted-foreground">Use the admin name and password for this clinic.</p>

          {failure ? (
            <div
              role="alert"
              className="mt-6 flex gap-2.5 rounded-lg border border-destructive/25 bg-destructive/5 px-4 py-3 text-sm text-destructive"
            >
              {failure.limited ? (
                <Clock className="mt-0.5 size-4 shrink-0" aria-hidden />
              ) : (
                <CircleAlert className="mt-0.5 size-4 shrink-0" aria-hidden />
              )}
              {failure.text}
            </div>
          ) : null}

          <form onSubmit={submit} className="mt-6 space-y-5">
            <div className="space-y-2">
              <Label htmlFor="user">User name</Label>
              <Input
                id="user"
                name="username"
                autoComplete="username"
                autoCapitalize="none"
                spellCheck={false}
                required
                maxLength={64}
                value={user}
                onChange={(event) => setUser(event.target.value)}
                className="h-11"
              />
            </div>
            <div className="space-y-2">
              <Label htmlFor="password">Password</Label>
              <Input
                id="password"
                name="password"
                type="password"
                autoComplete="current-password"
                required
                maxLength={256}
                value={password}
                onChange={(event) => setPassword(event.target.value)}
                className="h-11"
              />
            </div>
            <Button type="submit" className="h-11 w-full text-[0.95rem]" disabled={signIn.isPending}>
              {signIn.isPending ? "Signing in…" : "Sign in"}
            </Button>
          </form>

          <p className="mt-6 flex gap-2 text-sm text-muted-foreground">
            <Lock className="mt-0.5 size-4 shrink-0" aria-hidden />
            Sign out when you leave the desk. A session ends after 30 minutes without use, and after 8 hours
            in any case.
          </p>
        </div>
      </main>
    </div>
  );
}
