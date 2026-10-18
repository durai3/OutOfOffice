import type { Metadata } from "next";
import { Geist, JetBrains_Mono } from "next/font/google";
import "./globals.css";
import { AppShell } from "@/components/app-shell";
import { AuthGate } from "@/components/auth/auth-gate";
import { AuthProvider } from "@/lib/auth";
import { ThemeProvider } from "@/components/theme-provider";
import { ToastProvider } from "@/components/toast";

const geistSans = Geist({ subsets: ["latin"], variable: "--font-geist-sans" });
const geistMono = JetBrains_Mono({ subsets: ["latin"], variable: "--font-geist-mono" });

export const metadata: Metadata = {
  title: { default: "Out of Office Console", template: "%s \u00b7 Out of Office" },
  description: "Operate your Out of Office AI meeting delegate: dispatch, monitor, and govern it.",
};

export default function RootLayout({ children }: Readonly<{ children: React.ReactNode }>) {
  return (
    <html lang="en" suppressHydrationWarning>
      <body className={`${geistSans.variable} ${geistMono.variable} min-h-screen antialiased`}>
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
