// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import { humanizeProject, projectGraphHash, TaskLabel, taskLabelText } from "./TaskLabel";

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
  it("renders a project chip linking to the task graph and the bare title", () => {
    render(<TaskLabel title="Corrigir tracking de usage" project="argus" />);
    const chip = screen.getByRole("link", { name: "Open Argus task graph" });
    expect(chip.getAttribute("href")).toBe("#projects/argus/graph");
    expect(screen.getByText("Corrigir tracking de usage")).toBeTruthy();
    expect(document.body.textContent).not.toContain("Argus -");
    expect(taskLabelText("Corrigir tracking de usage", "argus")).toBe(
      "Argus · Corrigir tracking de usage",
    );
  });

  it("falls back to Global when project is null or absent", () => {
    render(
      <>
        <TaskLabel title="A" project={null} />
        <TaskLabel title="B" />
      </>,
    );
    expect(screen.getAllByRole("link", { name: "Open Global task graph" })).toHaveLength(2);
    expect(screen.getByText("A")).toBeTruthy();
    expect(screen.getByText("B")).toBeTruthy();
    expect(document.body.textContent).not.toContain("undefined");
    expect(projectGraphHash(null)).toBe("#projects/global/graph");
  });

  it("humanizes compound slugs on the chip", () => {
    render(<TaskLabel title="X" project="radar-web" />);
    expect(screen.getByRole("link", { name: "Open Radar Web task graph" }).textContent).toBe(
      "Radar Web",
    );
    expect(screen.getByText("X")).toBeTruthy();
  });

  it("opens the graph without bubbling when nested in a button", () => {
    const opened = { task: false };
    render(
      <button
        type="button"
        onClick={() => {
          opened.task = true;
        }}
      >
        <TaskLabel title="Ship it" project="radar" />
      </button>,
    );
    fireEvent.click(screen.getByRole("link", { name: "Open Radar task graph" }));
    expect(opened.task).toBe(false);
    expect(window.location.hash).toBe("#projects/radar/graph");
  });

  it("can hide the project chip when the caller places it elsewhere", () => {
    render(<TaskLabel title="Ship it" project="radar" projectChip={false} />);
    expect(screen.queryByRole("link", { name: "Open Radar task graph" })).toBeNull();
    expect(screen.getByText("Ship it")).toBeTruthy();
  });
});
