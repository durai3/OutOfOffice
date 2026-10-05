import type { Metadata } from "next";
import { PolicyEditor } from "@/components/policy/policy-editor";

export const metadata: Metadata = { title: "Representation Policy" };

export default function PolicyPage() {
  return <PolicyEditor />;
}
