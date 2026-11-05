import type { SettingValue } from "./settings";

export type GitIdentity = { name: string; email: string };

/** Same bounds and address shape as the `git.identity` schema (settings.spec.md 3.8). */
export const GIT_NAME_MAX = 200;
export const GIT_EMAIL_MAX = 320;
const EMAIL_SHAPE = /^[^\s@]+@[^\s@]+$/;

/** The stored identity, or null when the setting is unset (default `{}`). */
export function identityOf(setting?: SettingValue): GitIdentity | null {
  const v = setting?.value as Partial<GitIdentity> | null | undefined;
  return v?.name && v.email ? { name: v.name, email: v.email } : null;
}

/** Trimmed identity ready to save, or the first problem with the form. */
export function validateIdentity(
  name: string,
  email: string,
): { ok: true; identity: GitIdentity } | { ok: false; error: string } {
  const n = name.trim();
  const e = email.trim();
  if (!n) return { ok: false, error: "Name is required." };
  if (n.length > GIT_NAME_MAX)
    return { ok: false, error: `Name is at most ${GIT_NAME_MAX} characters.` };
  if (!e) return { ok: false, error: "Email is required." };
  if (e.length > GIT_EMAIL_MAX)
    return { ok: false, error: `Email is at most ${GIT_EMAIL_MAX} characters.` };
  if (!EMAIL_SHAPE.test(e)) return { ok: false, error: "Email must look like name@host." };
  return { ok: true, identity: { name: n, email: e } };
}

/** Where a scope's effective identity comes from: a project override, the global value, or nothing. */
export type IdentityOrigin = "override" | "inherited" | "unset";

export function originOf(project: string | null, setting?: SettingValue): IdentityOrigin {
  if (!identityOf(setting)) return "unset";
  if (project === null) return "inherited";
  return setting?.source === "project" ? "override" : "inherited";
}
