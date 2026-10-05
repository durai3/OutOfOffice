"use client";

import * as React from "react";
import { Monitor, Moon, Sun } from "lucide-react";
import { useTheme } from "next-themes";
import { Button } from "@/components/ui/button";

const order = ["light", "dark", "system"] as const;

export function ThemeToggle() {
  const { theme, setTheme } = useTheme();
  const [mounted, setMounted] = React.useState(false);
  React.useEffect(() => setMounted(true), []);

  const current = (mounted ? theme : "system") ?? "system";
  const next = order[(order.indexOf(current as (typeof order)[number]) + 1) % order.length];
  const Icon = current === "dark" ? Moon : current === "light" ? Sun : Monitor;

  return (
    <Button variant="ghost" size="icon" onClick={() => setTheme(next)} aria-label={`Theme: ${current}. Switch to ${next}.`} title={`Theme: ${current}`}>
      <Icon className="size-4" />
    </Button>
  );
}
