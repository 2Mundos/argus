// @vitest-environment jsdom
import { cleanup, render, screen, within } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import type { Route } from "./App";
import { PrimaryNav } from "./PrimaryNav";

afterEach(cleanup);

const LABELS = ["Tasks", "Inbox", "Projects", "Knowledge", "Routines", "Workers", "Setup"];

function renderNav(route: Route, orchestrator?: boolean) {
  render(<PrimaryNav route={route} orchestrator={orchestrator} />);
  return within(screen.getByRole("navigation", { name: "Primary" }));
}

function currentLabels(route: Route): string[] {
  return renderNav(route)
    .getAllByRole("link")
    .filter((a) => a.getAttribute("aria-current") === "page")
    .map((a) => a.textContent ?? "");
}

describe("PrimaryNav accessibility", () => {
  it("exposes seven links named by their labels, with decorative icons hidden", () => {
    const nav = renderNav({ tab: "tasks", taskId: null, filter: { states: [], q: "" } });
    const links = nav.getAllByRole("link");
    expect(links.map((a) => a.textContent)).toEqual(LABELS);
    LABELS.forEach((label, i) => {
      expect(nav.getByRole("link", { name: label })).toBe(links[i]);
    });
    for (const link of links) {
      expect(link.querySelector("svg")?.getAttribute("aria-hidden")).toBe("true");
      // jsdom does not apply the stylesheet, so the CSS hiding of this text below 960px is not
      // verified here; the label must stay in the DOM and exposed so it remains the accessible name.
      const text = link.querySelector(".nav-text");
      expect(text?.textContent).toBeTruthy();
      expect(text?.closest("[aria-hidden]")).toBeNull();
    }
  });

  it("never lists Dashboard", () => {
    for (const orchestrator of [true, false, undefined]) {
      const nav = renderNav({ tab: "dashboard" }, orchestrator);
      expect(nav.queryByRole("link", { name: "Dashboard" })).toBeNull();
      cleanup();
    }
  });

  it("lists Chat first only when the orchestrator is enabled", () => {
    const on = renderNav({ tab: "chat" }, true);
    expect(on.getAllByRole("link")[0]?.textContent).toBe("Chat");
    expect(on.getByRole("link", { name: "Chat" }).getAttribute("aria-current")).toBe("page");
    cleanup();
    for (const orchestrator of [false, undefined]) {
      const nav = renderNav({ tab: "chat" }, orchestrator);
      expect(nav.queryByRole("link", { name: "Chat" })).toBeNull();
      cleanup();
    }
  });

  it("marks only Workers current on a workers sub-page", () => {
    expect(currentLabels({ tab: "workers", sub: "pools" })).toEqual(["Workers"]);
  });

  it("marks only Setup current on a setup sub-page", () => {
    expect(currentLabels({ tab: "setup", sub: "accounts" })).toEqual(["Setup"]);
  });

  it("marks only Tasks current on a task detail", () => {
    expect(currentLabels({ tab: "tasks", taskId: "t1", filter: { states: [], q: "" } })).toEqual([
      "Tasks",
    ]);
  });
});
