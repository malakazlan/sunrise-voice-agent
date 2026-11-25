import { FileQuestion } from "lucide-react";
import Link from "next/link";

export default function NotFound() {
  return (
    <main className="flex min-h-dvh flex-col items-center justify-center px-6 text-center">
      <span className="mb-4 flex size-12 items-center justify-center rounded-full bg-secondary text-secondary-foreground">
        <FileQuestion className="size-6" aria-hidden />
      </span>
      <h1 className="font-heading text-2xl font-semibold">Not found</h1>
      <p className="mt-2 max-w-sm text-muted-foreground">
        There is no page at this address. If you followed a link to a call, the id may be wrong.
      </p>
      <Link href="/" className="mt-6 font-medium text-primary underline underline-offset-4">
        Go to Live calls
      </Link>
    </main>
  );
}
