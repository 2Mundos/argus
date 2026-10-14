import { describe, expect, it } from "vitest";
import { bytes, relative } from "./format";

describe("relative", () => {
  const now = Date.parse("2026-01-01T12:00:00Z");
  it("formats past and future times", () => {
    expect(relative("2026-01-01T11:57:00Z", now)).toMatch(/3 minutes ago/);
    expect(relative("2026-01-01T12:00:40Z", now)).toMatch(/in 40 seconds/);
  });
  it("handles missing values", () => {
    expect(relative(null, now)).toBe("—");
  });
});

describe("bytes", () => {
  it("picks a unit", () => {
    expect(bytes(512)).toBe("512 B");
    expect(bytes(2048)).toBe("2.0 KB");
  });
});

describe("actor", () => {
  it("shortens agent ids and leaves others alone", async () => {
    const { actor } = await import("./format");
    expect(actor("agent:09990f46-a760-46cc")).toBe("agent 09990f46");
    expect(actor("admin:preset")).toBe("admin:preset");
  });
});
