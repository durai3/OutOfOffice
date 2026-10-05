import type { Metadata } from "next";
import { Suspense } from "react";
import { HistoryView } from "@/components/history/history-view";
import { Skeleton } from "@/components/ui/skeleton";

export const metadata: Metadata = { title: "Meeting Briefs" };

export default function HistoryPage() {
  return (
    <Suspense fallback={<Skeleton className="h-64" />}>
      <HistoryView />
    </Suspense>
  );
}
