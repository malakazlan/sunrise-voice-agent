"use client";

import { ToggleGroup, ToggleGroupItem } from "@/components/ui/toggle-group";

// One choice out of a few, as a row of pills that wraps on a narrow screen.
export function FilterPills<T extends string>({
  label,
  value,
  options,
  onChange,
}: {
  label: string;
  value: T;
  options: readonly { value: T; label: string }[];
  onChange: (value: T) => void;
}) {
  return (
    <ToggleGroup
      type="single"
      aria-label={label}
      value={value}
      onValueChange={(next) => {
        if (next !== "") {
          onChange(next as T);
        }
      }}
      className="flex w-full flex-wrap justify-start gap-2"
    >
      {options.map((option) => (
        <ToggleGroupItem
          key={option.value}
          value={option.value}
          className="h-9 rounded-full border border-border bg-card px-3.5 text-sm font-medium text-foreground hover:bg-muted data-[state=on]:border-primary data-[state=on]:bg-primary data-[state=on]:text-primary-foreground"
        >
          {option.label}
        </ToggleGroupItem>
      ))}
    </ToggleGroup>
  );
}
