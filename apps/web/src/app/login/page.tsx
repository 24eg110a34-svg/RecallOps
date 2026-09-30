"use client";

import Link from "next/link";
import { useState } from "react";
import { api } from "@/lib/api";

const EMAIL_RE = /^[^\s@]+@[^\s@.]+(\.[^\s@.]+)+$/;

/**
 * Sign-in page.
 *
 * The form calls the real FastAPI endpoint and, on success, performs a full
 * navigation so the HttpOnly session cookie is picked up by the session gate.
 * A client-side push would leave the previous (anonymous) render in place.
 */
export default function LoginPage() {
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [showPassword, setShowPassword] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [fieldErrors, setFieldErrors] = useState<{ email?: string; password?: string }>({});
  const [busy, setBusy] = useState(false);

  const nextTarget = () => new URLSearchParams(window.location.search).get("next") || "/";

  const validate = () => {
    const errors: { email?: string; password?: string } = {};
    if (!email.trim()) errors.email = "Enter your email address.";
    if (!password) errors.password = "Enter your password.";
    setFieldErrors(errors);
    return Object.keys(errors).length === 0;
  };

  const submit = async (event: React.FormEvent) => {
    event.preventDefault();
    setError(null);
    if (busy) return; // guard against double submission
    if (!validate()) return;

    setBusy(true);
    try {
      await api.login(email.trim(), password);
      window.location.assign(nextTarget());
    } catch (e) {
      setError((e as Error).message);
      setBusy(false);
    }
  };

  const input =
    "w-full rounded-lg border bg-ink-850 px-3 py-2 text-sm text-slate-100 outline-none focus:border-signal-cyan/60";
  const label = "mb-1 block text-[11px] font-semibold uppercase tracking-wider text-slate-400";

  return (
    <div className="flex min-h-screen items-center justify-center px-4">
      <div className="w-full max-w-sm">
        <div className="mb-6 text-center">
          <div className="text-lg font-semibold tracking-tight text-white">
            RECALL<span className="text-signal-cyan">OPS</span>
          </div>
          <p className="mt-1 text-[13px] text-slate-400">Incident intelligence console</p>
        </div>

        <form onSubmit={submit} noValidate className="panel space-y-3 p-5">
          <div>
            <label htmlFor="login-email" className={label}>
              Email
            </label>
            <input
              id="login-email"
              name="email"
              type="email"
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              autoComplete="username"
              autoFocus
              aria-invalid={Boolean(fieldErrors.email)}
              aria-describedby={fieldErrors.email ? "login-email-error" : undefined}
              className={`${input} ${fieldErrors.email ? "border-signal-red/60" : "border-ink-700"}`}
              placeholder="you@company.com"
            />
            {fieldErrors.email && (
              <p id="login-email-error" className="mt-1 text-[11px] text-signal-red">
                {fieldErrors.email}
              </p>
            )}
          </div>

          <div>
            <label htmlFor="login-password" className={label}>
              Password
            </label>
            <div className="relative">
              <input
                id="login-password"
                name="password"
                type={showPassword ? "text" : "password"}
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                autoComplete="current-password"
                aria-invalid={Boolean(fieldErrors.password)}
                aria-describedby={fieldErrors.password ? "login-password-error" : undefined}
                className={`${input} pr-16 ${fieldErrors.password ? "border-signal-red/60" : "border-ink-700"}`}
                placeholder="••••••••"
              />
              <button
                type="button"
                onClick={() => setShowPassword((v) => !v)}
                className="absolute right-2 top-1/2 -translate-y-1/2 text-[11px] text-slate-400 hover:text-slate-200"
                aria-label={showPassword ? "Hide password" : "Show password"}
              >
                {showPassword ? "Hide" : "Show"}
              </button>
            </div>
            {fieldErrors.password && (
              <p id="login-password-error" className="mt-1 text-[11px] text-signal-red">
                {fieldErrors.password}
              </p>
            )}
          </div>

          {error && (
            <div role="alert" className="rounded-lg border border-signal-red/40 bg-signal-red/10 px-3 py-2 text-[12px] text-signal-red">
              {error}
            </div>
          )}

          <button type="submit" disabled={busy} className="btn-primary w-full">
            {busy ? "Signing in…" : "Sign in"}
          </button>
        </form>

        <p className="mt-4 text-center text-[12px] text-slate-400">
          Don&apos;t have an account?{" "}
          <Link href="/register" className="text-signal-cyan underline underline-offset-2 hover:opacity-80">
            Create one
          </Link>
        </p>

        <p className="mt-3 text-center text-[11px] leading-relaxed text-slate-500">
          Password recovery is not implemented on this deployment.
          <br />
          An operator can reset your password from the API log.
        </p>
      </div>
    </div>
  );
}
