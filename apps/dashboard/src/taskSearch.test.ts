import { describe, expect, it } from "vitest";
import { boundedSnippet, SNIPPET_MAX } from "./taskSearch";

describe("boundedSnippet", () => {
  it("bounds a snippet whatever the server sent", () => {
    expect(boundedSnippet("short")).toBe("short");
    expect(boundedSnippet("x".repeat(SNIPPET_MAX + 50))).toHaveLength(SNIPPET_MAX + 1);
  });
});
