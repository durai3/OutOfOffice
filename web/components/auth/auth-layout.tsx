"use client";

import * as React from "react";
import Link from "next/link";
import { APP_NAME } from "@/lib/config";

/** The centred card the sign-in and sign-up forms sit in. */
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
    <div className="grid min-h-screen place-items-center px-4 py-10">
      <div className="w-full max-w-md">
        <div className="mb-6 flex items-center gap-2.5">
          <span className="grid size-9 place-items-center rounded-lg bg-primary text-primary-foreground" aria-hidden="true">
            <span className="text-base font-bold leading-none tracking-tight">D8</span>
          </span>
          <div className="leading-tight">
            <div className="text-base font-semibold tracking-tight">{APP_NAME}</div>
            <div className="text-[11px] uppercase tracking-wider text-muted-foreground">Delegate console</div>
          </div>
        </div>
        <div className="rounded-xl border border-border bg-card p-6 shadow-sm">
          <h1 className="text-lg font-semibold tracking-tight">{title}</h1>
          <p className="mt-1 text-sm text-muted-foreground">{subtitle}</p>
          <div className="mt-5">{children}</div>
        </div>
        {footer ? <div className="mt-4 text-center text-sm text-muted-foreground">{footer}</div> : null}
        <p className="mt-6 text-center text-xs text-muted-foreground">
          By using DELEG8 you agree to the{" "}
          <Link href="/legal/terms" className="underline hover:text-foreground">Terms of Service</Link> and{" "}
          <Link href="/legal/privacy" className="underline hover:text-foreground">Privacy Policy</Link>.
        </p>
      </div>
    </div>
  );
}
