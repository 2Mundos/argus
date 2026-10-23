// @vitest-environment jsdom
import { render } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { lineTone, parseAnsiLine, renderTerminalText } from "./terminalLog";

describe("parseAnsiLine", () => {
  it("splits SGR colors into classed segments", () => {
    expect(parseAnsiLine("\x1b[31mfail\x1b[0m ok")).toEqual([
      { text: "fail", className: "term-fg-red" },
      { text: " ok", className: undefined },
    ]);
  });
});

describe("lineTone", () => {
  it("marks error and warn lines without ANSI", () => {
    expect(lineTone("ERROR boom")).toBe("term-fg-red");
    expect(lineTone("WARN slow")).toBe("term-fg-yellow");
    expect(lineTone("\x1b[31mERROR\x1b[0m")).toBeUndefined();
  });
});

describe("renderTerminalText", () => {
  it("renders one row per line", () => {
    const { container } = render(<>{renderTerminalText("a\nb")}</>);
    expect(container.querySelectorAll(".term-line")).toHaveLength(2);
  });
});
