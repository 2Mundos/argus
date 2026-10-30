import { describe, expect, it } from "vitest";
import { bytes, relative, utcIso } from "./format";

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

describe("utcIso", () => {
  it("normalizes to UTC ISO-8601", () => {
    expect(utcIso("2026-01-01T12:00:00+00:00")).toBe("2026-01-01T12:00:00.000Z");
    expect(utcIso(null)).toBeNull();
    expect(utcIso("not-a-date")).toBeNull();
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

describe("eventWorker", () => {
  it("shows a short worker id and labels other actors", async () => {
    const { eventWorker } = await import("./format");
    expect(eventWorker("agent:09990f46-a760-46cc")).toBe("09990f46");
    expect(eventWorker("admin:preset")).toBe("admin preset");
    expect(eventWorker("system")).toBe("system");
  });
});
