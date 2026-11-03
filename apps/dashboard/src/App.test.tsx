import { describe, expect, it } from "vitest";
import { defaultHash } from "./App";

describe("defaultHash", () => {
  it("rewrites an empty or invalid hash by the default route rule", () => {
    for (const hash of ["", "#", "#bogus", "#bogus/x"]) {
      expect(defaultHash(hash, true)).toBe("#chat");
      expect(defaultHash(hash, false)).toBe("#dashboard");
    }
  });

  it("keeps known and legacy routes", () => {
    for (const hash of ["#dashboard", "#chat", "#tasks/t1", "#workers/bogus", "#home", "#agents"]) {
      expect(defaultHash(hash, true)).toBeNull();
    }
  });
});
