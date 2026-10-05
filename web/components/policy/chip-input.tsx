"use client";

import * as React from "react";
import { X } from "lucide-react";
import { cn } from "@/lib/utils";

interface ChipInputProps {
  value: string[];
  onChange: (value: string[]) => void;
  placeholder?: string;
  normalise?: (raw: string) => string;
  id?: string;
  "aria-invalid"?: boolean;
}

export function ChipInput({ value, onChange, placeholder, normalise = (s) => s.trim(), id, ...aria }: ChipInputProps) {
  const [draft, setDraft] = React.useState("");
  const inputRef = React.useRef<HTMLInputElement>(null);

  function commit(raw: string) {
    const pieces = raw
      .split(/[,\n]/)
      .map(normalise)
      .filter(Boolean);
    if (!pieces.length) return;
    const next = [...value];
    for (const piece of pieces) if (!next.includes(piece)) next.push(piece);
    onChange(next);
    setDraft("");
  }

  return (
    <div
      className={cn(
        "flex min-h-9 w-full flex-wrap items-center gap-1.5 rounded-md border border-input bg-card px-2 py-1.5 text-sm shadow-sm",
        "focus-within:ring-2 focus-within:ring-ring",
        aria["aria-invalid"] && "border-danger",
      )}
      onClick={() => inputRef.current?.focus()}
    >
      {value.map((chip) => (
        <span key={chip} className="inline-flex items-center gap-1 rounded bg-muted px-1.5 py-0.5 text-xs">
          {chip}
          <button
            type="button"
            className="rounded text-muted-foreground hover:text-foreground"
            onClick={(e) => {
              e.stopPropagation();
              onChange(value.filter((v) => v !== chip));
            }}
            aria-label={`Remove ${chip}`}
          >
            <X className="size-3" />
          </button>
        </span>
      ))}
      <input
        ref={inputRef}
        id={id}
        value={draft}
        onChange={(e) => setDraft(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === "Enter" || e.key === ",") {
            e.preventDefault();
            commit(draft);
          } else if (e.key === "Backspace" && !draft && value.length) {
            onChange(value.slice(0, -1));
          }
        }}
        onBlur={() => draft.trim() && commit(draft)}
        onPaste={(e) => {
          const text = e.clipboardData.getData("text");
          if (text.includes(",") || text.includes("\n")) {
            e.preventDefault();
            commit(text);
          }
        }}
        placeholder={value.length ? "" : placeholder}
        className="min-w-[8ch] flex-1 bg-transparent text-sm outline-none placeholder:text-muted-foreground"
      />
    </div>
  );
}
