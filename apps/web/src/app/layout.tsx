import type { Metadata } from "next";
import "./globals.css";
import { Nav, ModeProvider } from "@/components/ui";

export const metadata: Metadata = {
  title: "RecallOps - AI Incident Intelligence",
  description:
    "AI incident response agent with Hindsight persistent memory: evidence, hypotheses, approval-gated actions, postmortems and organisational learning.",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body>
        <div className="mx-auto min-h-screen max-w-[1500px] px-4 py-4">
          <header className="mb-4 flex flex-wrap items-center justify-between gap-3 border-b border-ink-700/60 pb-3">
            <div className="flex items-center gap-4">
              <span className="text-base font-bold tracking-tight text-white">RECALL<span className="text-signal-cyan">OPS</span></span>
              <Nav />
            </div>
            <ModeProvider>
              <span className="text-[11px] text-slate-500">AI Incident Intelligence</span>
            </ModeProvider>
          </header>
          <main className="space-y-4 pb-10">{children}</main>
        </div>
      </body>
    </html>
  );
}
