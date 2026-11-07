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
    expect(parseRoute()).toEqual({ tab: "tasks", taskId: "abc", filter: { states: [] } });
    window.location.hash = "#inbox";
    expect(parseRoute()).toEqual({ tab: "inbox" });
    window.location.hash = "#projects";
    expect(parseRoute()).toEqual({ tab: "projects" });
  });
});

describe("tasks filter route", () => {
  it("keeps bare #tasks unfiltered", () => {
    window.location.hash = "#tasks";
    expect(parseRoute()).toEqual({ tab: "tasks", taskId: null, filter: { states: [] } });
  });

  it("reads repeatable states, deduplicated in canonical order", () => {
    window.location.hash = "#tasks?state=failed&state=blocked&state=failed";
    expect(parseRoute()).toEqual({
      tab: "tasks",
      taskId: null,
      filter: { states: ["blocked", "failed"] },
    });
    expect(tasksHash({ states: ["blocked", "failed"] })).toBe("#tasks?state=blocked&state=failed");
    expect(tasksHash({ states: [] })).toBe("#tasks");
  });

  it("falls back to unfiltered for unknown, empty or oversized values", () => {
    const none = { states: [] };
    expect(parseTaskFilter("#tasks?state=bogus")).toEqual(none);
    expect(parseTaskFilter("#tasks?state=blocked&state=bogus")).toEqual(none);
    expect(parseTaskFilter("#tasks?state=")).toEqual(none);
    expect(parseTaskFilter("#tasks?state=%E0%A4%A")).toEqual(none);
    expect(parseTaskFilter(`#tasks?state=${"blocked".repeat(100)}`)).toEqual(none);
    expect(parseTaskFilter(`#tasks?${"state=failed&".repeat(40)}`)).toEqual(none);
  });

  it("is not rewritten as an invalid hash and keeps a task id", () => {
    expect(defaultHash("#tasks?state=blocked", true)).toBeNull();
    window.location.hash = "#tasks/abc?state=failed";
    expect(parseRoute()).toMatchObject({ tab: "tasks", taskId: "abc" });
  });
});
