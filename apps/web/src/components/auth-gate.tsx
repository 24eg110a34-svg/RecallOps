"use client";

import { usePathname, useRouter } from "next/navigation";
import { useEffect, useState } from "react";
import { ModeProvider, Nav } from "@/components/ui";
import { api, type AuthSession } from "@/lib/api";

/**
 * Session gate for the whole console.
 *
 * Centralised here so the rule is applied once: `/login` renders bare, every
 * other route requires a valid session when the API asks for one. Doing this per
 * page would let a new page ship unprotected.
 */
export function AuthGate({ children }: { children: React.ReactNode }) {
  const pathname = usePathname();
  const router = useRouter();
  // Both auth pages render without the console chrome.
  const isBareRoute = pathname === "/login" || pathname === "/register";

  const [session, setSession] = useState<AuthSession | null>(null);
  const [checked, setChecked] = useState(false);

  useEffect(() => {
    let cancelled = false;
    api
      .session()
      .then((s) => {
        if (!cancelled) setSession(s);
      })
      .catch(() => {
        // API unreachable: fall back to "not required" so the health pages can
        // still explain what is wrong instead of bouncing to a login form.
        if (!cancelled)
          setSession({
            auth_required: false,
            authenticated: false,
            username: null,
            operators_configured: false,
            user: null,
            // Unknown while offline: hide the sign-up link rather than offer a
            // form that cannot succeed.
            registration_enabled: false,
            min_password_length: 8,
          });
      })
      .finally(() => {
        if (!cancelled) setChecked(true);
      });
    return () => {
      cancelled = true;
    };
  }, []);

  useEffect(() => {
    if (!checked || !session || isBareRoute) return;
    if (session.auth_required && !session.authenticated) {
      router.replace(`/login?next=${encodeURIComponent(pathname)}`);
    }
  }, [checked, session, isBareRoute, pathname, router]);

  if (isBareRoute) return <>{children}</>;

  // Never flash incident data before the session is known.
  if (!checked || !session || (session.auth_required && !session.authenticated)) {
    return (
      <div className="flex min-h-screen items-center justify-center">
        <p className="text-[13px] text-slate-500">Checking session…</p>
      </div>
    );
  }

  return (
    <div className="mx-auto min-h-screen max-w-[1500px] px-4 py-4">
      <header className="mb-4 flex flex-wrap items-center justify-between gap-3 border-b border-ink-700/60 pb-3">
        <div className="flex items-center gap-4">
          <span className="text-base font-bold tracking-tight text-white">
            RECALL<span className="text-signal-cyan">OPS</span>
          </span>
          <Nav />
        </div>
        <div className="flex items-center gap-3">
          <ModeProvider>
            <span className="text-[11px] text-slate-500">AI Incident Intelligence</span>
          </ModeProvider>
          {session.auth_required && (
            <div className="flex items-center gap-2 border-l border-ink-700/60 pl-3">
              <span className="chip border-ink-600 bg-ink-800 text-slate-300">{session.username}</span>
              <button
                className="btn-ghost"
                onClick={async () => {
                  try {
                    await api.logout();
                  } finally {
                    window.location.assign("/login");
                  }
                }}
              >
                Sign out
              </button>
            </div>
          )}
        </div>
      </header>
      <main className="space-y-4 pb-10">{children}</main>
    </div>
  );
}
