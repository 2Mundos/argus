import { describe, expect, it } from "vitest";
import { envPatch } from "./pools";

describe("envPatch", () => {
  const stored = { TOKEN: "***", MODE: "***" };

  it("sends nothing when every value is left redacted", () => {
    expect(envPatch(stored, { TOKEN: "***", MODE: "***" })).toBeUndefined();
  });

  it("sets changed and new values and removes dropped keys", () => {
    expect(envPatch(stored, { MODE: "slow", NEW: "1" })).toEqual({
      MODE: "slow",
      NEW: "1",
      TOKEN: null,
    });
  });

  it("sends a literal *** for a key that was not stored before", () => {
    expect(envPatch({}, { MASK: "***" })).toEqual({ MASK: "***" });
  });
});
