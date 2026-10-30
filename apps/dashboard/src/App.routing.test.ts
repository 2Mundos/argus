// @vitest-environment jsdom
import { afterEach, describe, expect, it } from "vitest";
import { parseRoute } from "./App";

afterEach(() => {
  window.location.hash = "";
});

describe("parseRoute", () => {
  it("defaults to dashboard", () => {
    window.location.hash = "";
    expect(parseRoute()).toEqual({ tab: "dashboard" });
    window.location.hash = "#bogus";
    expect(parseRoute()).toEqual({ tab: "dashboard" });
  });

  it("redirects #home to #dashboard", () => {
    window.location.hash = "#home";
    expect(parseRoute()).toEqual({ tab: "dashboard" });
    expect(window.location.hash).toBe("#dashboard");
  });

  it("keeps other routes", () => {
    window.location.hash = "#tasks/abc";
    expect(parseRoute()).toEqual({ tab: "tasks", taskId: "abc" });
    window.location.hash = "#inbox";
    expect(parseRoute()).toEqual({ tab: "inbox" });
  });
});
