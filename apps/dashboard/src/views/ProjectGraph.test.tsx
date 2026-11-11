// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ProjectGraph } from "./ProjectGraph";

vi.hoisted(() => {
  const Base = globalThis.Request;
  globalThis.Request = class extends Base {
    constructor(input: RequestInfo | URL, init?: RequestInit) {
      super(
        typeof input === "string" && input.startsWith("/") ? `http://localhost${input}` : input,
        init,
      );
    }
  } as typeof Request;
  const make = (id: string, state: string, failed: string[] = []) => ({
    id,
    title: `Task ${id}`,
    state,
    kind: "generic",
    priority: 0,
    block_reason: null,
    failed_dependencies: failed,
    project: "web",
  });
  const graph = {
    nodes: [
      make("a", "succeeded"),
      make("b", "pending"),
      make("c", "cancelled"),
      make("d", "pending", ["c"]),
      make("x", "ready"),
    ],
    edges: [
      { from: "a", to: "b" },
      { from: "c", to: "d" },
    ],
    truncated: true,
  };
  globalThis.fetch = (async () =>
    new Response(JSON.stringify(graph), {
      headers: { "Content-Type": "application/json" },
    })) as typeof fetch;
});

vi.mock("../live", async () => {
  const { useEffect, useState } = await import("react");
  return {
    useLiveData: <T,>(load: (signal: AbortSignal) => Promise<T>) => {
      const [data, setData] = useState<T>();
      useEffect(() => {
        load(new AbortController().signal).then(setData);
      }, []);
      return { data, error: undefined, reload: () => {} };
    },
  };
});

afterEach(cleanup);

describe("ProjectGraph", () => {
  it("links nodes to task detail, marks stuck ones and highlights lineage on hover", async () => {
    const { container } = render(<ProjectGraph project="web" />);
    await waitFor(() => expect(container.querySelectorAll("a.tg-node")).toHaveLength(5));
    expect(screen.getByRole("status").textContent).toContain("Only the newest tasks");

    const link = (id: string) => container.querySelector(`a[href="#tasks/${id}"]`) as Element;
    expect(link("d").classList.contains("is-stuck")).toBe(true);
    expect(link("b").classList.contains("is-stuck")).toBe(false);

    fireEvent.mouseEnter(link("b"));
    expect(link("a").classList.contains("is-on")).toBe(true);
    expect(link("b").classList.contains("is-on")).toBe(true);
    expect(link("x").classList.contains("is-dim")).toBe(true);
    expect(link("d").classList.contains("is-dim")).toBe(true);
    fireEvent.mouseLeave(link("b"));
    expect(container.querySelector(".is-dim")).toBeNull();
  });
});
