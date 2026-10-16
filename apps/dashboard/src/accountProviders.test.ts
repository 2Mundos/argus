import { describe, expect, it } from "vitest";
import { CUSTOM_PROVIDER_PATTERN } from "./accountProviders";

describe("CUSTOM_PROVIDER_PATTERN", () => {
  it("compiles with Unicode v and matches the backend id rules in Chrome", () => {
    const re = new RegExp(`^(?:${CUSTOM_PROVIDER_PATTERN})$`, "v");
    expect(re.test("Bad Value")).toBe(false);
    expect(re.test("Bad")).toBe(false);
    expect(re.test("my_provider-1")).toBe(true);
    expect(re.test("openai")).toBe(true);
  });
});
