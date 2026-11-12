// @vitest-environment jsdom
import { describe, expect, it } from "vitest";
import { defaultHash, homeHash, isCurrent, parseRoute } from "./App";

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

describe("menu", () => {
  it("links the brand to the page the app opens on", () => {
    expect(homeHash(true)).toBe("#chat");
    expect(homeHash(false)).toBe("#dashboard");
    expect(defaultHash("", true)).toBe(homeHash(true));
  });

  it("marks a grouped entry current on any of its tabs, and only those", () => {
    for (const [hash, current] of [
      ["#workers/pools", "#workers"],
      ["#workers", "#workers"],
      ["#setup/cost", "#setup"],
      ["#setup", "#setup"],
      ["#tasks/t1", "#tasks"],
    ] as const) {
      window.location.hash = hash;
      const route = parseRoute();
      const all = ["#tasks", "#workers", "#setup", "#chat"].filter((href) =>
        isCurrent(href, route),
      );
      expect(all).toEqual([current]);
    }
  });

  it("opens the first tab of a group and keeps old deep links", () => {
    for (const [hash, expected] of [
      ["#workers", { tab: "workers", sub: "agents" }],
      ["#setup", { tab: "setup", sub: "settings" }],
      ["#accounts", { tab: "setup", sub: "accounts" }],
      ["#setup/integrations", { tab: "setup", sub: "integrations" }],
      ["#frota", { tab: "workers", sub: "fleet" }],
    ] as const) {
      window.location.hash = hash;
      expect(parseRoute()).toEqual(expected);
    }
  });
});
