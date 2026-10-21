// @vitest-environment jsdom
import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import { Markdown } from "./Markdown";

afterEach(cleanup);

describe("Markdown", () => {
  it("renders raw HTML as text, not markup", () => {
    render(<Markdown>{"hello <script>alert(1)</script> world"}</Markdown>);
    expect(document.querySelector("script")).toBeNull();
    expect(screen.getByText(/hello/)).toBeTruthy();
    expect(screen.getByText(/alert\(1\)/)).toBeTruthy();
    expect(screen.getByText(/world/)).toBeTruthy();
  });
});
