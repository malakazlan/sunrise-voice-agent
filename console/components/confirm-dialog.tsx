"use client";

import type { ReactNode } from "react";

import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from "@/components/ui/alert-dialog";
import { ApiError } from "@/lib/api/client";

// The one confirmation every write goes through: it says what will happen in plain words and
// names the action on its button. Nothing is sent until that button is pressed; "Cancel" (or
// Escape, or a click outside) sends nothing. Focus starts on "Cancel", never on the action, so a
// stray Enter does not confirm, and a destructive action is drawn as one.
export function ConfirmDialog({
  open,
  onOpenChange,
  title,
  description,
  action,
  destructive = false,
  onConfirm,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  title: string;
  description: ReactNode;
  action: string;
  destructive?: boolean;
  onConfirm: () => void;
}) {
  return (
    <AlertDialog open={open} onOpenChange={onOpenChange}>
      <AlertDialogContent className="data-[size=default]:max-w-md data-[size=default]:sm:max-w-md">
        <AlertDialogHeader>
          <AlertDialogTitle className="text-lg font-semibold">{title}</AlertDialogTitle>
          <AlertDialogDescription className="text-[0.95rem] text-foreground/80">
            {description}
          </AlertDialogDescription>
        </AlertDialogHeader>
        <AlertDialogFooter>
          <AlertDialogCancel className="h-10 px-4">Cancel</AlertDialogCancel>
          <AlertDialogAction
            className={destructive ? "h-10 px-4 ring-1 ring-destructive/40 ring-inset" : "h-10 px-4"}
            variant={destructive ? "destructive" : "default"}
            onClick={onConfirm}
          >
            {action}
          </AlertDialogAction>
        </AlertDialogFooter>
      </AlertDialogContent>
    </AlertDialog>
  );
}

// What a refused write says. A 403 means the anti-forgery check no longer matches this session; a
// 429 means the API's per-session limit, with how long it asks to wait.
export function writeFailure(error: unknown, otherwise: string): string {
  if (error instanceof ApiError && error.kind === "forbidden") {
    return "Your session has changed. Sign in again.";
  }
  if (error instanceof ApiError && error.kind === "rate_limited") {
    const wait = error.retryAfterSeconds;
    return `Too many requests. Try again in ${wait === null ? "a moment" : `${wait} seconds`}.`;
  }
  return otherwise;
}
