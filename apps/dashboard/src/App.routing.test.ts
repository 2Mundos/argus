// @vitest-environment jsdom
import { afterEach, describe, expect, it } from "vitest";
import { defaultHash, parseRoute } from "./App";
import { parseTaskFilter, tasksHash } from "./taskRoute";

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
    expect(parseRoute()).toEqual({ tab: "tasks", taskId: "abc", filter: { states: [], q: "" } });
    window.location.hash = "#inbox";
    expect(parseRoute()).toEqual({ tab: "inbox" });
    window.location.hash = "#projects";
    expect(parseRoute()).toEqual({ tab: "projects" });
  });

  it("keeps the tasks route when it carries a search", () => {
    window.location.hash = "#tasks?q=a%2Fb";
    expect(parseRoute()).toEqual({
      tab: "tasks",
      taskId: null,
      filter: { states: [], q: "a/b" },
    });
    expect(defaultHash("#tasks?q=a%2Fb", true)).toBeNull();
    window.location.hash = "#home?x=1";
    expect(parseRoute()).toEqual({ tab: "dashboard" });
    expect(window.location.hash).toBe("#dashboard?x=1");
  });
});

describe("tasks filter route", () => {
  it("keeps bare #tasks unfiltered", () => {
    window.location.hash = "#tasks";
    expect(parseRoute()).toEqual({ tab: "tasks", taskId: null, filter: { states: [], q: "" } });
  });

  it("reads repeatable states, deduplicated in canonical order", () => {
    window.location.hash = "#tasks?state=failed&state=blocked&state=failed";
    expect(parseRoute()).toEqual({
      tab: "tasks",
      taskId: null,
      filter: { states: ["blocked", "failed"], q: "" },
    });
    expect(tasksHash({ states: ["blocked", "failed"], q: "" })).toBe(
      "#tasks?state=blocked&state=failed",
    );
    expect(tasksHash({ states: [], q: "" })).toBe("#tasks");
  });

  it("falls back to unfiltered for unknown, empty or oversized values", () => {
    const none = { states: [], q: "" };
    expect(parseTaskFilter("#tasks?state=bogus")).toEqual(none);
    expect(parseTaskFilter("#tasks?state=blocked&state=bogus")).toEqual(none);
    expect(parseTaskFilter("#tasks?state=")).toEqual(none);
    expect(parseTaskFilter("#tasks?state=%E0%A4%A")).toEqual(none);
    expect(parseTaskFilter(`#tasks?state=${"blocked".repeat(100)}`)).toEqual(none);
    expect(parseTaskFilter(`#tasks?${"state=failed&".repeat(40)}`)).toEqual(none);
  });

  it("composes states and q in one encoding that round-trips", () => {
    for (const q of ["retry", "a/b & c=d", "ação ✓", "50% ?x#y"]) {
      const filter = { states: ["blocked" as const, "failed" as const], q };
      expect(parseTaskFilter(tasksHash(filter))).toEqual(filter);
      expect(parseTaskFilter(tasksHash({ q }))).toEqual({ states: [], q });
    }
    expect(tasksHash({ states: ["failed"], q: " retry " })).toBe("#tasks?state=failed&q=retry");
    expect(tasksHash({ q: "  " })).toBe("#tasks");
    expect(parseTaskFilter("#tasks?state=bogus&q=x")).toEqual({ states: [], q: "x" });
    expect(parseTaskFilter(`#tasks?q=${"x".repeat(600)}`).q).toHaveLength(600);
  });

  it("is not rewritten as an invalid hash and keeps a task id", () => {
    expect(defaultHash("#tasks?state=blocked", true)).toBeNull();
    window.location.hash = "#tasks/abc?state=failed";
    expect(parseRoute()).toMatchObject({ tab: "tasks", taskId: "abc" });
  });
});
