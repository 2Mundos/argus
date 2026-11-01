import { describe, expect, it } from "vitest";
import type { Account } from "./api";
import { credentialLabel, rejectedSinceAccepted } from "./authHealth";
import { attention } from "./overview";

const T1 = "2026-01-01T10:00:00Z";
const T2 = "2026-01-01T11:00:00Z";

describe("rejectedSinceAccepted", () => {
  it.each([
    [null, null, false],
    [T1, null, false],
    [null, T1, true],
    [T1, T2, true],
    [T2, T1, false],
    [T1, T1, false],
  ])("accepted %s, rejected %s -> %s", (accepted, rejected, want) => {
    expect(rejectedSinceAccepted({ last_accepted_at: accepted, last_rejected_at: rejected })).toBe(
      want,
    );
  });
});

describe("credentialLabel", () => {
  it("never says healthy", () => {
    expect(credentialLabel(null)).toBeNull();
    expect(credentialLabel(undefined)).toBeNull();
    expect(credentialLabel({ state: "missing" })).toBe("missing");
    expect(credentialLabel({ state: "expired", expires_at: T1 })).toBe("expired");
    expect(credentialLabel({ state: "present" })).toBe("present, expiry unknown");
    expect(credentialLabel({ state: "present", expires_at: T2 })).toBe("present, expiry as stored");
  });
});

describe("attention for rejected accounts", () => {
  const overview = { data: undefined, error: "n/a" };
  const system = { data: undefined, error: "n/a" };
  const account = (id: string, accepted: string | null, rejected: string | null) =>
    ({ id, name: id, last_accepted_at: accepted, last_rejected_at: rejected }) as Account;

  it("flags only an account whose rejection is newer than its acceptance", () => {
    const a = attention({
      stream: "live",
      overview,
      system,
      accounts: {
        data: [account("bad", T1, T2), account("fine", T2, T1), account("quiet", null, null)],
      },
    });
    const ids = a.items.map((i) => i.id).filter((id) => id.startsWith("auth-rejected-"));
    expect(ids).toEqual(["auth-rejected-bad"]);
  });
});
