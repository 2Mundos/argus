// @vitest-environment jsdom
import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import { humanizeProject, TaskLabel } from "./TaskLabel";

afterEach(cleanup);

describe("humanizeProject", () => {
  it("capitalizes slugs", () => {
    expect(humanizeProject("argus")).toBe("Argus");
    expect(humanizeProject("radar-web")).toBe("Radar Web");
    expect(humanizeProject("global")).toBe("Global");
  });
  it("falls back to Global", () => {
    for (const v of [null, undefined, ""]) expect(humanizeProject(v)).toBe("Global");
  });
});

describe("TaskLabel", () => {
  it("renders project and title, full text in title attribute", () => {
    render(<TaskLabel title="Corrigir tracking de usage" project="argus" />);
    const el = screen.getByText("Argus - Corrigir tracking de usage");
    expect(el.getAttribute("title")).toBe("Argus - Corrigir tracking de usage");
  });
  it("falls back to Global when project is null or absent", () => {
    render(
      <>
        <TaskLabel title="A" project={null} />
        <TaskLabel title="B" />
      </>,
    );
    expect(screen.getByText("Global - A")).toBeTruthy();
    expect(screen.getByText("Global - B")).toBeTruthy();
    expect(document.body.textContent).not.toContain("undefined");
  });
  it("humanizes compound slugs", () => {
    render(<TaskLabel title="X" project="radar-web" />);
    expect(screen.getByText("Radar Web - X")).toBeTruthy();
  });
});
