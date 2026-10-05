"use client";

import * as React from "react";
import Link from "next/link";
import { usePathname } from "next/navigation";
import {
  BookOpen,
  Gauge,
  History,
  LayoutDashboard,
  LogOut,
  Radio,
  Settings,
  ShieldCheck,
  UserRound,
  WifiOff,
} from "lucide-react";
import { api } from "@/lib/api";
import { API_BASE, APP_NAME } from "@/lib/config";
import { useApi } from "@/hooks/use-api";
import { useAuth } from "@/lib/auth";
import { isPublic } from "@/components/auth/auth-gate";
import { cn } from "@/lib/utils";
import { ThemeToggle } from "@/components/theme-toggle";
import { ActivityBadge } from "@/components/status-badge";
import { HealthBanner } from "@/components/health-banner";

const NAV = [
  { href: "/", label: "Overview", icon: LayoutDashboard },
  { href: "/live", label: "Live Monitor", icon: Radio },
  { href: "/policy", label: "Policy", icon: ShieldCheck },
  { href: "/dossier", label: "About Me", icon: UserRound },
  { href: "/knowledge", label: "Knowledge", icon: BookOpen },
  { href: "/history", label: "Briefs", icon: History },
  { href: "/usage", label: "Usage", icon: Gauge },
  { href: "/settings", label: "Settings", icon: Settings },
] as const;

function isActive(pathname: string, href: string): boolean {
  return href === "/" ? pathname === "/" : pathname === href || pathname.startsWith(`${href}/`);
}

export function AppShell({ children }: { children: React.ReactNode }) {
  const pathname = usePathname();
  const { status, account, logout } = useAuth();
  const signedIn = status === "authenticated";

  // The sign-in, sign-up, onboarding and legal pages carry their own layout.
  // The console chrome around them would be either empty or wrong.
  if (!signedIn || isPublic(pathname) || pathname === "/onboarding") {
    return <>{children}</>;
  }

  return <ConsoleShell pathname={pathname} name={account?.principal?.display_name ?? account?.display_name ?? ""} email={account?.email ?? ""} onLogout={logout}>{children}</ConsoleShell>;
}

function ConsoleShell({
  pathname,
  name,
  email,
  onLogout,
  children,
}: {
  pathname: string;
  name: string;
  email: string;
  onLogout: () => void;
  children: React.ReactNode;
}) {
  const health = useApi(api.health, { refreshMs: 15_000 });
  const delegate = useApi(api.delegate, { refreshMs: 5_000 });
  const online = !health.error && Boolean(health.data);

  return (
    <div className="flex min-h-screen">
      <aside className="hidden w-60 shrink-0 flex-col border-r border-border bg-card/60 lg:flex">
        <div className="flex h-14 items-center gap-2.5 px-5">
          <Logo />
          <div className="leading-tight">
            <div className="text-sm font-semibold tracking-tight">{APP_NAME}</div>
            <div className="text-[10px] uppercase tracking-wider text-muted-foreground">Delegate console</div>
          </div>
        </div>
        <nav className="flex flex-1 flex-col gap-0.5 px-3 py-2" aria-label="Primary">
          {NAV.map(({ href, label, icon: Icon }) => {
            const active = isActive(pathname, href);
            return (
              <Link
                key={href}
                href={href}
                aria-current={active ? "page" : undefined}
                className={cn(
                  "flex items-center gap-2.5 rounded-md px-2.5 py-2 text-sm transition-colors",
                  active ? "bg-accent font-medium text-foreground" : "text-muted-foreground hover:bg-accent/60 hover:text-foreground",
                )}
              >
                <Icon className="size-4" />
                {label}
                {href === "/live" && delegate.data?.active ? (
                  <span className="ml-auto size-1.5 rounded-full bg-success pulse-dot" aria-hidden="true" />
                ) : null}
              </Link>
            );
          })}
        </nav>
        <div className="border-t border-border p-4">
          <div className="truncate text-xs font-medium">{name}</div>
          <div className="truncate text-[11px] text-muted-foreground" title={email}>{email}</div>
          <button
            type="button"
            onClick={onLogout}
            className="mt-2 flex items-center gap-1.5 text-[11px] text-muted-foreground transition-colors hover:text-foreground"
          >
            <LogOut className="size-3" aria-hidden="true" /> Sign out
          </button>
          <div className="mt-3 flex items-center gap-2 border-t border-border pt-3 text-xs text-muted-foreground">
            <span className={cn("size-1.5 rounded-full", online ? "bg-success" : "bg-danger")} aria-hidden="true" />
            {online ? `API v${health.data?.version}` : "API offline"}
          </div>
          <div className="mt-1 truncate font-mono text-[10px] text-muted-foreground" title={API_BASE}>
            {API_BASE.replace(/^https?:\/\//, "")}
          </div>
        </div>
      </aside>

      <div className="flex min-w-0 flex-1 flex-col">
        <header className="sticky top-0 z-40 flex h-14 items-center gap-3 border-b border-border bg-background/80 px-4 backdrop-blur sm:px-6">
          <div className="flex items-center gap-2 lg:hidden">
            <Logo />
            <span className="text-sm font-semibold">{APP_NAME}</span>
          </div>
          <nav className="scroll-thin -mx-1 flex flex-1 items-center gap-1 overflow-x-auto px-1 lg:hidden" aria-label="Primary">
            {NAV.map(({ href, label }) => (
              <Link
                key={href}
                href={href}
                className={cn(
                  "whitespace-nowrap rounded-md px-2.5 py-1.5 text-xs",
                  isActive(pathname, href) ? "bg-accent font-medium" : "text-muted-foreground hover:text-foreground",
                )}
              >
                {label}
              </Link>
            ))}
          </nav>
          <div className="ml-auto flex items-center gap-2">
            {health.data && !health.data.healthy ? (
              <span
                className="hidden items-center gap-1.5 rounded-full border border-warning/40 px-2.5 py-1 text-xs text-warning sm:flex"
                title={health.data.banner ?? undefined}
              >
                Degraded
              </span>
            ) : null}
            {health.error ? (
              <span className="hidden items-center gap-1.5 rounded-full border border-danger/40 px-2.5 py-1 text-xs text-danger sm:flex" title={health.error}>
                <WifiOff className="size-3.5" /> Backend unreachable
              </span>
            ) : null}
            {delegate.data ? (
              <Link href="/live" className="rounded-full focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring">
                <ActivityBadge activity={delegate.data.activity} />
              </Link>
            ) : null}
            <button
              type="button"
              onClick={onLogout}
              className="rounded-md p-1.5 text-muted-foreground transition-colors hover:bg-accent hover:text-foreground lg:hidden"
              aria-label="Sign out"
            >
              <LogOut className="size-4" />
            </button>
            <ThemeToggle />
          </div>
        </header>
        <main className="flex-1 px-4 py-6 sm:px-6 lg:px-8">
          <div className="mx-auto w-full max-w-7xl">
            <HealthBanner health={health.data} onRefresh={health.refresh} />
            {children}
          </div>
        </main>
      </div>
    </div>
  );
}

function Logo() {
  return (
    <span className="grid size-7 place-items-center rounded-md bg-primary text-primary-foreground" aria-hidden="true">
      <span className="text-[13px] font-bold leading-none tracking-tight">D8</span>
    </span>
  );
}
