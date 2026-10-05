import type { Metadata } from "next";
import { DossierView } from "@/components/dossier/dossier-view";

export const metadata: Metadata = { title: "About Me" };

export default function DossierPage() {
  return <DossierView />;
}
