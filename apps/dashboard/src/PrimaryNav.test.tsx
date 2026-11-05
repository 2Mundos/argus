// @vitest-environment jsdom
import { cleanup, render, screen, within } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import type { Route } from "./App";
import { PrimaryNav } from "./PrimaryNav";

afterEach(cleanup);

const LABELS = [
  "Chat",
  "Dashboard",
  "Tasks",
  "Inbox",
  "Projects",
  "Knowledge",
  "Routines",
  "Workers",
  "Setup",
];

function renderNav(route: Route) {
  render(<PrimaryNav route={route} />);
  return within(screen.getByRole("navigation", { name: "Primary" }));
}

function currentLabels(route: Route): string[] {
  return renderNav(route)
    .getAllByRole("link")
    .filter((a) => a.getAttribute("aria-current") === "page")
    .map((a) => a.textContent ?? "");
}

describe("PrimaryNav accessibility", () => {
  it("exposes nine links named by their labels, with decorative icons hidden", () => {
    const nav = renderNav({ tab: "chat" });
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

  it("marks only Workers current on a workers sub-page", () => {
    expect(currentLabels({ tab: "workers", sub: "pools" })).toEqual(["Workers"]);
  });

  it("marks only Setup current on a setup sub-page", () => {
    expect(currentLabels({ tab: "setup", sub: "accounts" })).toEqual(["Setup"]);
  });

  it("marks only Tasks current on a task detail", () => {
    expect(currentLabels({ tab: "tasks", taskId: "t1" })).toEqual(["Tasks"]);
  });
});
