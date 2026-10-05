import type { Metadata } from "next";
import { LiveView } from "@/components/live/live-view";

export const metadata: Metadata = { title: "Live Monitor" };

export default function LivePage() {
  return <LiveView />;
}
