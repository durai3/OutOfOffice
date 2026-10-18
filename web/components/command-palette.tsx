"use client";

import * as React from "react";
import { Search } from "lucide-react";
import { type LucideIcon } from "lucide-react";
import { cn } from "@/lib/utils";

export interface CommandItem {
  id: string;
  label: string;
  icon: LucideIcon;
  hint?: string;
  run: () => void;
}

/**
 * Global ⌘K / Ctrl+K palette. Every entry is a real action (route navigation,
 * theme toggle, sign out) - there is nothing here that looks actionable but
 * does nothing, on purpose.
 */
export function CommandPalette({
  open,
  onClose,
  items,
}: {
  open: boolean;
  onClose: () => void;
  items: CommandItem[];
}) {
  const [query, setQuery] = React.useState("");
  const [selected, setSelected] = React.useState(0);
  const inputRef = React.useRef<HTMLInputElement>(null);

  const filtered = React.useMemo(() => {
    const q = query.trim().toLowerCase();
    if (!q) return items;
    return items.filter((item) => item.label.toLowerCase().includes(q));
  }, [items, query]);

  React.useEffect(() => {
    if (open) {
      setQuery("");
      setSelected(0);
      requestAnimationFrame(() => inputRef.current?.focus());
    }
  }, [open]);

  React.useEffect(() => {
    setSelected(0);
  }, [query]);

  if (!open) return null;

  function run(item: CommandItem | undefined) {
    if (!item) return;
    onClose();
    item.run();
  }

  function onKeyDown(event: React.KeyboardEvent<HTMLInputElement>) {
    if (event.key === "Escape") {
      event.preventDefault();
      onClose();
    } else if (event.key === "ArrowDown") {
      event.preventDefault();
      setSelected((current) => Math.min(filtered.length - 1, current + 1));
    } else if (event.key === "ArrowUp") {
      event.preventDefault();
      setSelected((current) => Math.max(0, current - 1));
    } else if (event.key === "Enter") {
      event.preventDefault();
      run(filtered[selected]);
    }
  }

  return (
    <div
      className="fixed inset-0 z-[80] flex items-start justify-center bg-black/45 pt-[12vh] fade-in"
      onClick={onClose}
    >
      <div
        onClick={(event) => event.stopPropagation()}
        className="w-[min(580px,92vw)] overflow-hidden rounded-xl border border-border bg-card shadow-2xl"
      >
        <div className="flex h-[52px] items-center gap-2.5 border-b border-border px-4">
          <Search className="size-[18px] shrink-0 text-muted-foreground" />
          <input
            ref={inputRef}
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            onKeyDown={onKeyDown}
            placeholder="Search screens and commands"
            spellCheck={false}
            className="h-full flex-1 bg-transparent text-[15px] text-foreground outline-none placeholder:text-muted-foreground"
          />
          <span className="rounded-md border border-border px-1.5 py-0.5 font-mono text-[10.5px] leading-none text-muted-foreground">
            ESC
          </span>
        </div>
        <div className="scroll-thin max-h-[364px] overflow-y-auto p-1.5">
          {filtered.length === 0 ? (
            <div className="py-7 text-center text-[13.5px] text-muted-foreground">No matches.</div>
          ) : (
            filtered.map((item, index) => {
              const Icon = item.icon;
              const active = index === selected;
              return (
                <div
                  key={item.id}
                  onMouseEnter={() => setSelected(index)}
                  onClick={() => run(item)}
                  className={cn(
                    "flex h-[38px] cursor-pointer items-center gap-3 rounded-md px-2.5",
                    active ? "bg-primary/10" : "hover:bg-accent",
                  )}
                >
                  <Icon className={cn("size-[17px]", active ? "text-primary" : "text-muted-foreground")} />
                  <span className="flex-1 text-[13.5px] font-medium">{item.label}</span>
                  {item.hint ? (
                    <span className="font-mono text-[11px] text-muted-foreground">{item.hint}</span>
                  ) : null}
                </div>
              );
            })
          )}
        </div>
        <div className="flex gap-4 border-t border-border bg-muted/40 px-4 py-2 font-mono text-[11px] text-muted-foreground">
          <span>↑↓ navigate</span>
          <span>↵ run</span>
          <span className="ml-auto">⌘K toggle</span>
        </div>
      </div>
    </div>
  );
}
