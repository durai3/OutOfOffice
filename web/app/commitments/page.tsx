import type { Metadata } from "next";
import { CommitmentsTrackerView } from "@/components/commitments/commitments-tracker-view";

export const metadata: Metadata = { title: "Commitments" };

export default function CommitmentsPage() {
  return <CommitmentsTrackerView />;
}
