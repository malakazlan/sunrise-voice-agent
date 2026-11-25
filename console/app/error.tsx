"use client";

import { CircleAlert } from "lucide-react";

import { Button } from "@/components/ui/button";

// A page that failed to render. Nothing of the error is shown or logged here.
export default function ErrorPage({ reset }: { error: Error; reset: () => void }) {
  return (
    <main className="flex min-h-dvh flex-col items-center justify-center px-6 text-center">
      <span className="mb-4 flex size-12 items-center justify-center rounded-full bg-destructive/10 text-destructive">
        <CircleAlert className="size-6" aria-hidden />
      </span>
      <h1 className="font-heading text-2xl font-semibold">This page could not be shown</h1>
      <p className="mt-2 max-w-sm text-muted-foreground">
        Something went wrong while showing it. Nothing was saved or changed.
      </p>
      <Button className="mt-6" onClick={reset}>
        Try again
      </Button>
    </main>
  );
}
