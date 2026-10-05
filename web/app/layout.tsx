import type { Metadata } from "next";
import "./globals.css";
import { AppShell } from "@/components/app-shell";
import { AuthGate } from "@/components/auth/auth-gate";
import { AuthProvider } from "@/lib/auth";
import { ThemeProvider } from "@/components/theme-provider";
import { ToastProvider } from "@/components/toast";

export const metadata: Metadata = {
  title: { default: "DELEG8 Console", template: "%s \u00b7 DELEG8" },
  description: "Operate your DELEG8 AI meeting delegate: dispatch, monitor, and govern it.",
};

export default function RootLayout({ children }: Readonly<{ children: React.ReactNode }>) {
  return (
    <html lang="en" suppressHydrationWarning>
      <body className="min-h-screen antialiased">
        <ThemeProvider>
          <ToastProvider>
            <AuthProvider>
              <AuthGate>
                <AppShell>{children}</AppShell>
              </AuthGate>
            </AuthProvider>
          </ToastProvider>
        </ThemeProvider>
      </body>
    </html>
  );
}
