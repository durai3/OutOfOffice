"use client";

import * as React from "react";
import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { useTheme } from "next-themes";
import {
  BarChart3,
  BookOpen,
  Calendar,
  Gauge,
  History,
  LayoutDashboard,
  ListChecks,
  LogOut,
  Monitor,
  Moon,
  PanelLeftClose,
  FlaskConical,
  Plug,
  Radio,
  Search,
  Settings,
  ShieldCheck,
  Sun,
  Users,
  UserRound,
  type LucideIcon,
} from "lucide-react";
import { api } from "@/lib/api";
import { API_BASE, APP_NAME } from "@/lib/config";
import { useApi } from "@/hooks/use-api";
import { useAuth } from "@/lib/auth";
import { isPublic } from "@/components/auth/auth-gate";
import { cn } from "@/lib/utils";
import { HealthBanner } from "@/components/health-banner";
import { CommandPalette, type CommandItem } from "@/components/command-palette";

type NavLink = { href: string; label: string; icon: LucideIcon };

// Primary: what you need before and during a meeting. Grouped below: what
// fills in once you've actually used the delegate (Insights), and account
// admin (Account) - kept out of the primary list so a brand-new account with
// no meetings yet isn't staring at eight empty tabs.
const NAV_PRIMARY: NavLink[] = [
  { href: "/", label: "Overview", icon: LayoutDashboard },
  { href: "/playground", label: "Playground", icon: FlaskConical },
  { href: "/calendar", label: "Calendar", icon: Calendar },
  { href: "/live", label: "Live Monitor", icon: Radio },
  { href: "/policy", label: "Policy", icon: ShieldCheck },
  { href: "/dossier", label: "About Me", icon: UserRound },
  { href: "/knowledge", label: "Knowledge", icon: BookOpen },
];

const NAV_GROUPS: { label: string; items: NavLink[] }[] = [
  {
    label: "Insights",
    items: [
      { href: "/history", label: "Briefs", icon: History },
      { href: "/commitments", label: "Commitments", icon: ListChecks },
      { href: "/analytics", label: "Analytics", icon: BarChart3 },
    ],
  },
  {
    label: "Account",
    items: [
      { href: "/connectors", label: "Connectors", icon: Plug },
      { href: "/usage", label: "Usage", icon: Gauge },
      { href: "/organization", label: "Team", icon: Users },
      { href: "/settings", label: "Settings", icon: Settings },
    ],
  },
];

const NAV_ALL: NavLink[] = [...NAV_PRIMARY, ...NAV_GROUPS.flatMap((group) => group.items)];

// First 9 routes get a ⌘ digit; the rest get ⇧⌘ - mirrors how a command
// palette's own shortcut list is usually laid out.
const NAV_SHORTCUTS = NAV_ALL.map((item, index) =>
  index < 9 ? { ...item, combo: `⌘${index + 1}`, digit: String(index + 1), shift: false } : { ...item, combo: `⇧⌘${index - 8}`, digit: String(index - 8), shift: true },
);

const SIDEBAR_KEY = "deleg8:sidebar-open";

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

  return (
    <ConsoleShell pathname={pathname} name={account?.principal?.display_name ?? account?.display_name ?? ""} email={account?.email ?? ""} onLogout={logout}>
      {children}
    </ConsoleShell>
  );
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
  const router = useRouter();
  const health = useApi(api.health, { refreshMs: 15_000 });
  const delegate = useApi(api.delegate, { refreshMs: 5_000 });
  const online = !health.error && Boolean(health.data);
  const degraded = Boolean(health.data && !health.data.healthy);

  const [sbOpen, setSbOpen] = React.useState(true);
  const [cmdOpen, setCmdOpen] = React.useState(false);

  React.useEffect(() => {
    try {
      const stored = localStorage.getItem(SIDEBAR_KEY);
      if (stored !== null) setSbOpen(stored === "1");
    } catch {
      // localStorage unavailable (private mode, etc.) - default stays open.
    }
  }, []);

  function toggleSidebar() {
    setSbOpen((current) => {
      const next = !current;
      try {
        localStorage.setItem(SIDEBAR_KEY, next ? "1" : "0");
      } catch {
        // best-effort persistence only
      }
      return next;
    });
  }

  React.useEffect(() => {
    function onKeyDown(event: KeyboardEvent) {
      if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "k") {
        event.preventDefault();
        setCmdOpen((current) => !current);
        return;
      }
      if (event.key === "Escape" && cmdOpen) {
        setCmdOpen(false);
        return;
      }
      if (!(event.metaKey || event.ctrlKey)) return;
      const match = /^Digit(\d)$/.exec(event.code);
      if (!match) return;
      const entry = NAV_SHORTCUTS.find((item) => item.digit === match[1] && item.shift === event.shiftKey);
      if (entry) {
        event.preventDefault();
        router.push(entry.href);
      }
    }
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [cmdOpen, router]);

  const activeLabel = NAV_ALL.find((item) => isActive(pathname, item.href))?.label ?? "";

  const commandItems: CommandItem[] = [
    ...NAV_ALL.map((item): CommandItem => ({
      id: `go-${item.href}`,
      label: `Go to ${item.label}`,
      icon: item.icon,
      hint: NAV_SHORTCUTS.find((s) => s.href === item.href)?.combo,
      run: () => router.push(item.href),
    })),
    { id: "sign-out", label: "Sign out", icon: LogOut, run: onLogout },
  ];

  return (
    <div className="flex min-h-screen">
      <Sidebar
        open={sbOpen}
        onToggle={toggleSidebar}
        pathname={pathname}
        live={Boolean(delegate.data?.active)}
        name={name}
        email={email}
        onLogout={onLogout}
      />

      <div className="flex min-w-0 flex-1 flex-col">
        <header className="sticky top-0 z-40 flex h-[54px] shrink-0 items-center gap-4 border-b border-border bg-card px-4 sm:px-6">
          <nav className="scroll-thin -mx-1 flex flex-1 items-center gap-1 overflow-x-auto px-1 lg:hidden" aria-label="Primary">
            {NAV_ALL.map(({ href, label }) => (
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

          <div className="hidden min-w-0 flex-shrink items-center gap-2 overflow-hidden whitespace-nowrap font-mono text-xs text-muted-foreground lg:flex">
            <span>{APP_NAME}</span>
            <span>/</span>
            <span className="text-foreground">{activeLabel}</span>
          </div>

          <button
            type="button"
            onClick={() => setCmdOpen(true)}
            className="mx-auto hidden h-[34px] min-w-0 flex-1 items-center gap-2.5 rounded-md border border-border bg-muted/60 px-3 text-left text-sm text-muted-foreground transition-colors hover:border-ring hover:bg-muted sm:flex sm:max-w-[440px]"
          >
            <Search className="size-4 shrink-0" />
            <span className="flex-1">Search &amp; commands</span>
            <span className="rounded-md border border-border bg-card px-1.5 py-0.5 font-mono text-[11px] leading-none text-muted-foreground">⌘K</span>
          </button>

          <div className="ml-auto flex shrink-0 items-center gap-2">
            <span
              className="hidden items-center gap-2 rounded-md border border-border px-2.5 py-1 font-mono text-[11.5px] text-muted-foreground sm:flex"
              title={health.error ?? health.data?.banner ?? undefined}
            >
              <span className={cn("size-[7px] rounded-full", !online ? "bg-danger" : degraded ? "bg-warning" : "bg-success")} />
              {!online ? "Offline" : degraded ? "Degraded" : "Online"}
            </span>
            <button
              type="button"
              onClick={onLogout}
              className="rounded-md p-1.5 text-muted-foreground transition-colors hover:bg-accent hover:text-foreground lg:hidden"
              aria-label="Sign out"
            >
              <LogOut className="size-4" />
            </button>
          </div>
        </header>

        <main className="flex-1 px-4 py-6 sm:px-6 lg:px-9">
          <div className="mx-auto w-full max-w-7xl">
            <HealthBanner health={health.data} onRefresh={health.refresh} />
            {children}
          </div>
        </main>
      </div>

      <CommandPalette open={cmdOpen} onClose={() => setCmdOpen(false)} items={commandItems} />
    </div>
  );
}

function Sidebar({
  open,
  onToggle,
  pathname,
  live,
  name,
  email,
  onLogout,
}: {
  open: boolean;
  onToggle: () => void;
  pathname: string;
  live: boolean;
  name: string;
  email: string;
  onLogout: () => void;
}) {
  const initials = (name || email || "OO").trim().slice(0, 2).toUpperCase();

  return (
    <aside
      className={cn(
        "sticky top-0 hidden h-screen shrink-0 flex-col self-start overflow-hidden border-r border-black/40 bg-[linear-gradient(180deg,#151517_0%,#0e0e10_100%)] text-[#ededf0] transition-[width] duration-150 lg:flex",
        open ? "w-[248px]" : "w-[68px]",
      )}
    >
      <div
        aria-hidden="true"
        className="pointer-events-none absolute inset-0 bg-[radial-gradient(80%_28%_at_0%_0%,rgba(255,255,255,0.06),transparent_70%)]"
      />
      <div className="relative flex h-[60px] shrink-0 items-center gap-2.5 px-4">
        <Logo />
        {open ? (
          <>
            <div className="flex-1 font-semibold tracking-[0.14em]">
              OUT <span className="text-[#d8d8de]">OF OFFICE</span>
            </div>
            <button
              type="button"
              onClick={onToggle}
              title="Collapse sidebar"
              className="flex size-7 shrink-0 items-center justify-center rounded-md text-[#8a8a93] transition-colors hover:bg-white/[0.07] hover:text-white"
            >
              <PanelLeftClose className="size-[17px]" />
            </button>
          </>
        ) : null}
      </div>
      {!open ? (
        <button
          type="button"
          onClick={onToggle}
          title="Expand sidebar"
          className="relative mx-auto mb-1.5 flex h-7 w-8 items-center justify-center rounded-md text-[#8a8a93] transition-colors hover:bg-white/[0.07] hover:text-white"
        >
          <PanelLeftClose className="size-[17px]" />
        </button>
      ) : null}

      <nav className="relative flex flex-1 flex-col gap-0.5 overflow-y-auto px-2.5 py-2" aria-label="Primary">
        {NAV_PRIMARY.map((item) => (
          <SidebarItem key={item.href} item={item} active={isActive(pathname, item.href)} open={open} showLiveDot={item.href === "/live" && live} />
        ))}
        {NAV_GROUPS.map((group) => (
          <div key={group.label} className="mt-4">
            {open ? (
              <div className="px-2.5 pb-1.5 pt-2 font-mono text-[10.5px] uppercase tracking-[0.14em] text-[#63636c]">{group.label}</div>
            ) : (
              <div className="mx-3.5 my-2.5 h-px bg-white/[0.08]" />
            )}
            {group.items.map((item) => (
              <SidebarItem key={item.href} item={item} active={isActive(pathname, item.href)} open={open} />
            ))}
          </div>
        ))}
      </nav>

      <div className="relative flex shrink-0 flex-col gap-2.5 border-t border-white/[0.08] px-2.5 py-3.5">
        <ThemeSegment open={open} />
        <div className={cn("flex items-center gap-2.5 px-1", open ? "justify-start" : "justify-center")}>
          <div className="flex size-8 shrink-0 items-center justify-center rounded-full bg-[linear-gradient(140deg,#5a5a63,#2a2a2f)] text-xs font-semibold text-white">
            {initials}
          </div>
          {open ? (
            <>
              <div className="min-w-0 flex-1 leading-tight">
                <div className="truncate text-[13px] font-medium">{name || "Your account"}</div>
                <div className="truncate text-[11.5px] text-[#8a8a93]" title={email}>
                  {email}
                </div>
              </div>
              <button
                type="button"
                onClick={onLogout}
                title="Sign out"
                className="flex size-[30px] shrink-0 items-center justify-center rounded-md text-[#8a8a93] transition-colors hover:bg-white/[0.07] hover:text-white"
              >
                <LogOut className="size-[17px]" />
              </button>
            </>
          ) : null}
        </div>
        {open ? (
          <div className="truncate px-1 font-mono text-[10px] text-[#63636c]" title={API_BASE}>
            {API_BASE.replace(/^https?:\/\//, "")}
          </div>
        ) : null}
      </div>
    </aside>
  );
}

function SidebarItem({ item, active, open, showLiveDot }: { item: NavLink; active: boolean; open: boolean; showLiveDot?: boolean }) {
  const Icon = item.icon;
  const combo = NAV_SHORTCUTS.find((s) => s.href === item.href)?.combo;
  return (
    <Link
      href={item.href}
      aria-current={active ? "page" : undefined}
      title={item.label}
      className={cn(
        "flex h-9 items-center gap-2.5 rounded-md px-2.5 text-[13.5px] font-medium transition-colors",
        open ? "justify-start" : "justify-center",
        active ? "bg-white/[0.08] text-white" : "text-[#a4a4ad] hover:bg-white/[0.06]",
      )}
    >
      <Icon className={cn("size-[18px] shrink-0", active ? "text-white" : "text-[#7b7b84]")} />
      {open ? (
        <>
          <span className="flex-1 truncate">{item.label}</span>
          {showLiveDot ? (
            <span className="relative mr-0.5 size-[8px] shrink-0">
              <span className="absolute inset-0 rounded-full bg-success" />
              <span className="absolute inset-0 rounded-full bg-success pulse-dot" />
            </span>
          ) : combo ? (
            <span className="shrink-0 font-mono text-[10.5px] text-[#66666f]">{combo}</span>
          ) : null}
        </>
      ) : null}
    </Link>
  );
}

function ThemeSegment({ open }: { open: boolean }) {
  const { theme, setTheme } = useTheme();
  const [mounted, setMounted] = React.useState(false);
  React.useEffect(() => setMounted(true), []);
  const current = (mounted ? theme : "system") ?? "system";

  if (!open) {
    const order = ["light", "dark", "system"] as const;
    const next = order[(order.indexOf(current as (typeof order)[number]) + 1) % order.length];
    const Icon = current === "dark" ? Moon : current === "light" ? Sun : Monitor;
    return (
      <button
        type="button"
        onClick={() => setTheme(next)}
        title={`Theme: ${current}`}
        className="mx-auto flex size-8 items-center justify-center rounded-md bg-white/[0.05] text-[#ededf0]"
      >
        <Icon className="size-4" />
      </button>
    );
  }

  const options: { key: "light" | "dark" | "system"; icon: LucideIcon; label: string }[] = [
    { key: "light", icon: Sun, label: "Light" },
    { key: "dark", icon: Moon, label: "Dark" },
  ];

  return (
    <div className="flex gap-0.5 rounded-md bg-white/[0.05] p-[3px]">
      {options.map(({ key, icon: Icon, label }) => (
        <button
          key={key}
          type="button"
          onClick={() => setTheme(key)}
          className={cn(
            "flex h-[26px] flex-1 items-center justify-center gap-1.5 rounded-[7px] text-xs font-medium transition-colors",
            current === key ? "bg-white/[0.14] text-white" : "text-[#8a8a93]",
          )}
        >
          <Icon className="size-3.5" />
          {label}
        </button>
      ))}
    </div>
  );
}

function Logo() {
  return (
    <div className="flex w-[30px] shrink-0 items-center justify-center" aria-hidden="true">
      <div className="size-[16px] rounded-full border-2 border-[#d8d8de]" />
      <div className="-ml-1.5 size-[16px] rounded-full border-2 border-[#d8d8de] shadow-[0_0_14px_rgba(255,255,255,0.27)]" />
    </div>
  );
}
