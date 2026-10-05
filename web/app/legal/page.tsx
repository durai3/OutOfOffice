import type { Metadata } from "next";
import { LegalIndex } from "@/components/legal/legal-index";

export const metadata: Metadata = { title: "Legal" };

export default function LegalPage() {
  return <LegalIndex />;
}
