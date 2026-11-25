import { Fragment } from "react";

// Text exactly as the API masked it. What the masking put in (a name tag such as <PERSON>, or the
// stars of a masked date, phone number or SSN) is set in a chip so that it reads as masked. Never
// reformatted, never filled in.
const MASK = /(<[A-Z_]+>|\*[\d*-]*)/g;

export function MaskedText({ text }: { text: string }) {
  const parts = text.split(MASK);
  return (
    <>
      {parts.map((part, index) =>
        index % 2 === 1 ? (
          <span
            key={index}
            title="Masked"
            className="mx-0.5 rounded-sm bg-muted px-1 py-px font-mono text-[0.85em] text-muted-foreground ring-1 ring-border ring-inset"
          >
            {part}
          </span>
        ) : (
          <Fragment key={index}>{part}</Fragment>
        ),
      )}
    </>
  );
}
