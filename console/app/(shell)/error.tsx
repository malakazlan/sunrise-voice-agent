"use client";

import { ErrorState } from "@/components/states";
import { Card } from "@/components/ui/card";

// A page inside the shell that failed to render: an error card in the page's place, with the menu
// and the top bar still there. Nothing of the error is shown or logged here.
export default function ShellError({ error, reset }: { error: Error; reset: () => void }) {
  return (
    <Card className="py-0">
      <ErrorState error={error} onRetry={reset} />
    </Card>
  );
}
