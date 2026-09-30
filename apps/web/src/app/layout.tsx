import type { Metadata } from "next";
import "./globals.css";
import { AuthGate } from "@/components/auth-gate";

export const metadata: Metadata = {
  title: "RecallOps - AI Incident Intelligence",
  description:
    "AI incident response agent with Hindsight persistent memory: evidence, hypotheses, approval-gated actions, postmortems and organisational learning.",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body>
        <AuthGate>{children}</AuthGate>
      </body>
    </html>
  );
}
