// Auth-health facts per account (docs/specs/account-usage.spec.md 3.5, 3.6). A
// timestamp is evidence of a past event, not a verdict; "present" is never "healthy".
import type { Account } from "./api";

type Facts = Pick<Account, "last_accepted_at" | "last_rejected_at">;

/** The last rejection is newer than the last acceptance, or nothing was ever accepted. */
export function rejectedSinceAccepted(a: Facts): boolean {
  if (!a.last_rejected_at) return false;
  if (!a.last_accepted_at) return true;
  return Date.parse(a.last_rejected_at) > Date.parse(a.last_accepted_at);
}

/** Wording for the stored credential; null where the account holds no model credential. */
export function credentialLabel(c: Account["credential"]): string | null {
  if (!c) return null;
  if (c.state === "missing") return "missing";
  if (c.state === "expired") return "expired";
  return c.expires_at ? "present, expiry as stored" : "present, expiry unknown";
}
