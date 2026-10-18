"use client";

import * as React from "react";
import Link from "next/link";
import { JudgeEntry } from "@/components/auth/judge-entry";
import { APP_NAME } from "@/lib/config";

/**
 * The centred card the sign-in and sign-up forms sit in.
 *
 * Deliberately dark regardless of the site's light/dark preference - this is
 * the first thing a new principal sees, and it should read as a premium,
 * trust-setting gate rather than a plain settings form. Only this outer shell
 * is forced dark (via the literal `dark` class); it has no bearing on the
 * theme the rest of the console renders in.
 */
export function AuthLayout({
  title,
  subtitle,
  children,
  footer,
}: {
  title: string;
  subtitle: string;
  children: React.ReactNode;
  footer?: React.ReactNode;
}) {
  return (
    <div className="dark relative isolate min-h-screen overflow-hidden bg-[linear-gradient(175deg,#17161c_0%,#0c0c0e_55%,#080808_100%)]">
      <div
        aria-hidden="true"
        className="pointer-events-none absolute inset-0 bg-[radial-gradient(55%_45%_at_50%_-5%,color-mix(in_oklch,var(--primary)_30%,transparent)_0%,transparent_70%)]"
      />
      <div
        aria-hidden="true"
        className="pointer-events-none absolute inset-0 bg-[linear-gradient(rgba(255,255,255,0.035)_1px,transparent_1px),linear-gradient(90deg,rgba(255,255,255,0.035)_1px,transparent_1px)] [background-size:32px_32px] [mask-image:radial-gradient(60%_55%_at_50%_15%,#000_0%,transparent_75%)]"
      />

      <div className="relative grid min-h-screen place-items-center px-4 py-12">
        <div className="w-full max-w-md">
          <div className="mb-7 flex flex-col items-center gap-2.5 text-center">
            <span
              className="grid size-10 place-items-center rounded-xl bg-primary text-primary-foreground shadow-[0_8px_24px_-8px_rgba(0,0,0,0.6)] ring-1 ring-white/10"
              aria-hidden="true"
            >
              <span className="text-base font-bold leading-none tracking-tight">OO</span>
            </span>
            <div className="leading-tight">
              <div className="text-base font-semibold tracking-tight text-foreground">{APP_NAME}</div>
              <div className="font-mono text-[10.5px] uppercase tracking-[0.12em] text-muted-foreground">
                Delegate console
              </div>
            </div>
          </div>

          <JudgeEntry className="mb-5" />

          <div className="rounded-2xl border border-border bg-card p-6 shadow-[0_30px_70px_-25px_rgba(0,0,0,0.7)] sm:p-7">
            <h1 className="text-lg font-semibold tracking-tight text-foreground">{title}</h1>
            <p className="mt-1.5 text-sm text-muted-foreground">{subtitle}</p>
            <div className="mt-5">{children}</div>
          </div>

          {footer ? <div className="mt-5 text-center text-sm text-muted-foreground">{footer}</div> : null}

          <p className="mt-6 text-center text-xs text-muted-foreground">
            Want the tour first?{" "}
            <Link href="/judges" className="font-medium text-foreground underline underline-offset-2">
              Read the judge guide
            </Link>
          </p>

          <p className="mt-4 text-center text-xs text-muted-foreground">
            By using Out of Office you agree to the{" "}
            <Link href="/legal/terms" className="underline decoration-muted-foreground/40 underline-offset-2 hover:text-foreground">
              Terms of Service
            </Link>{" "}
            and{" "}
            <Link href="/legal/privacy" className="underline decoration-muted-foreground/40 underline-offset-2 hover:text-foreground">
              Privacy Policy
            </Link>
            .
          </p>
        </div>
      </div>
    </div>
  );
}
