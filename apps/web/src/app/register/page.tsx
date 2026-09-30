"use client";

import Link from "next/link";
import { useState } from "react";
import { api } from "@/lib/api";

const EMAIL_RE = /^[^\s@]+@[^\s@.]+(\.[^\s@.]+)+$/;

/**
 * Self-service sign-up: full name, email, username, password.
 *
 * The username is derived from the email but stays editable, because the
 * timeline and action records reference an operator by username and operators
 * should not be forced into a handle they did not choose.
 *
 * On success the API signs the new operator in, so this page navigates straight
 * to the console rather than bouncing back through /login.
 */
export default function RegisterPage() {
  const [fullName, setFullName] = useState("");
  const [email, setEmail] = useState("");
  const [username, setUsername] = useState("");
  const [usernameTouched, setUsernameTouched] = useState(false);
  const [password, setPassword] = useState("");
  const [confirm, setConfirm] = useState("");
  const [showPassword, setShowPassword] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [fieldErrors, setFieldErrors] = useState<Record<string, string>>({});
  const [busy, setBusy] = useState(false);

  const nextTarget = () => new URLSearchParams(window.location.search).get("next") || "/";

  // Suggest a username from the email until the operator types their own.
  const suggestUsername = (value: string) => {
    const local = value.split("@")[0] ?? "";
    return local.replace(/[^a-zA-Z0-9._-]/g, "").slice(0, 32);
  };

  const onEmailChange = (value: string) => {
    setEmail(value);
    if (!usernameTouched) setUsername(suggestUsername(value));
  };

  const passwordProblems = [
    password.length > 0 && password.length < 8 ? "at least 8 characters" : null,
    password.length > 0 && password === username ? "not the same as your username" : null,
  ].filter(Boolean) as string[];

  const validate = () => {
    const errors: Record<string, string> = {};
    if (!fullName.trim()) errors.fullName = "Enter your full name.";
    if (!email.trim()) errors.email = "Enter your email address.";
    else if (!EMAIL_RE.test(email.trim())) errors.email = "Enter a valid email address.";
    if (!username.trim()) errors.username = "Choose a username.";
    else if (username.trim().length < 3) errors.username = "Username must be at least 3 characters.";
    if (!password) errors.password = "Choose a password.";
    else if (password.length < 8) errors.password = "Password must be at least 8 characters.";
    if (!confirm) errors.confirm = "Repeat your password.";
    else if (confirm !== password) errors.confirm = "Passwords do not match.";
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
      await api.register({
        full_name: fullName.trim(),
        email: email.trim(),
        username: username.trim(),
        password,
      });
      window.location.assign(nextTarget());
    } catch (e) {
      setError((e as Error).message);
      setBusy(false);
    }
  };

  const input =
    "w-full rounded-lg border bg-ink-850 px-3 py-2 text-sm text-slate-100 outline-none focus:border-signal-cyan/60";
  const label = "mb-1 block text-[11px] font-semibold uppercase tracking-wider text-slate-400";
  const invalid = (key: string) => `${input} ${fieldErrors[key] ? "border-signal-red/60" : "border-ink-700"}`;

  return (
    <div className="flex min-h-screen items-center justify-center px-4 py-8">
      <div className="w-full max-w-sm">
        <div className="mb-6 text-center">
          <div className="text-lg font-semibold tracking-tight text-white">
            RECALL<span className="text-signal-cyan">OPS</span>
          </div>
          <p className="mt-1 text-[13px] text-slate-400">Create an operator account</p>
        </div>

        <form onSubmit={submit} noValidate className="panel space-y-3 p-5">
          <div>
            <label htmlFor="reg-fullname" className={label}>
              Full name
            </label>
            <input
              id="reg-fullname"
              name="full_name"
              value={fullName}
              onChange={(e) => setFullName(e.target.value)}
              autoComplete="name"
              autoFocus
              className={invalid("fullName")}
              placeholder="Amaya Okonkwo"
            />
            {fieldErrors.fullName && <p className="mt-1 text-[11px] text-signal-red">{fieldErrors.fullName}</p>}
          </div>

          <div>
            <label htmlFor="reg-email" className={label}>
              Email
            </label>
            <input
              id="reg-email"
              name="email"
              type="email"
              value={email}
              onChange={(e) => onEmailChange(e.target.value)}
              autoComplete="email"
              className={invalid("email")}
              placeholder="you@company.com"
            />
            {fieldErrors.email && <p className="mt-1 text-[11px] text-signal-red">{fieldErrors.email}</p>}
          </div>

          <div>
            <label htmlFor="reg-username" className={label}>
              Username
            </label>
            <input
              id="reg-username"
              name="username"
              value={username}
              onChange={(e) => {
                setUsernameTouched(true);
                setUsername(e.target.value);
              }}
              autoComplete="username"
              className={invalid("username")}
              placeholder="amaya"
            />
            {fieldErrors.username ? (
              <p className="mt-1 text-[11px] text-signal-red">{fieldErrors.username}</p>
            ) : (
              <p className="mt-1 text-[11px] text-slate-500">Used to label your actions on the incident timeline.</p>
            )}
          </div>

          <div>
            <label htmlFor="reg-password" className={label}>
              Password
            </label>
            <div className="relative">
              <input
                id="reg-password"
                name="password"
                type={showPassword ? "text" : "password"}
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                autoComplete="new-password"
                className={`${invalid("password")} pr-16`}
                placeholder="at least 8 characters"
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
            {fieldErrors.password ? (
              <p className="mt-1 text-[11px] text-signal-red">{fieldErrors.password}</p>
            ) : passwordProblems.length > 0 ? (
              <p className="mt-1 text-[11px] text-slate-500">Password should be {passwordProblems[0]}.</p>
            ) : null}
          </div>

          <div>
            <label htmlFor="reg-confirm" className={label}>
              Confirm password
            </label>
            <input
              id="reg-confirm"
              name="confirm"
              type={showPassword ? "text" : "password"}
              value={confirm}
              onChange={(e) => setConfirm(e.target.value)}
              autoComplete="new-password"
              className={invalid("confirm")}
              placeholder="repeat the password"
            />
            {fieldErrors.confirm && <p className="mt-1 text-[11px] text-signal-red">{fieldErrors.confirm}</p>}
          </div>

          {error && (
            <div role="alert" className="rounded-lg border border-signal-red/40 bg-signal-red/10 px-3 py-2 text-[12px] text-signal-red">
              {error}
            </div>
          )}

          <button type="submit" disabled={busy} className="btn-primary w-full">
            {busy ? "Creating account…" : "Create account"}
          </button>
        </form>

        <p className="mt-4 text-center text-[12px] text-slate-400">
          Already have an account?{" "}
          <Link href="/login" className="text-signal-cyan underline underline-offset-2 hover:opacity-80">
            Sign in
          </Link>
        </p>
        <p className="mt-3 text-center text-[11px] text-slate-500">
          Anyone who can sign up can operate this console. Operators can be closed with{" "}
          <code className="text-slate-400">AUTH_REGISTRATION_ENABLED=false</code>.
        </p>
      </div>
    </div>
  );
}
