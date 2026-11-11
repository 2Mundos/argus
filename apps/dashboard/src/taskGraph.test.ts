import { describe, expect, it } from "vitest";
import { layers, lineage } from "./taskGraph";

const edge = (from: string, to: string) => ({ from, to });

describe("layers", () => {
  it("puts a task after the longest chain of its dependencies", () => {
    const { columns, column } = layers(
      ["d", "c", "b", "a", "x"],
      [edge("a", "b"), edge("b", "c"), edge("a", "c"), edge("c", "d")],
    );
    expect(Object.fromEntries(column)).toEqual({ a: 0, x: 0, b: 1, c: 2, d: 3 });
    expect(columns.map((c) => [...c].sort())).toEqual([["a", "x"], ["b"], ["c"], ["d"]]);
  });

  it("orders rows by the barycenter of their dependencies and survives a cycle", () => {
    const { columns } = layers(["p", "q", "b", "a"], [edge("p", "a"), edge("q", "b")]);
    expect(columns).toEqual([
      ["p", "q"],
      ["a", "b"],
    ]);
    expect(layers(["a", "b"], [edge("a", "b"), edge("b", "a")]).columns).toHaveLength(1);
  });
});

describe("lineage", () => {
  it("is the node, its ancestors and its descendants, not its siblings", () => {
    const edges = [edge("a", "b"), edge("b", "c"), edge("a", "s"), edge("z", "c")];
    expect([...lineage("b", edges)].sort()).toEqual(["a", "b", "c"]);
  });
});
