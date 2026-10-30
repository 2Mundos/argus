// @vitest-environment jsdom
import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { DateChip } from "./DateChip";

describe("DateChip", () => {
  const now = Date.parse("2026-01-01T12:00:00Z");

  it("shows relative text and UTC ISO on hover", () => {
    render(<DateChip at="2026-01-01T11:55:00Z" now={now} />);
    const chip = screen.getByText(/5 minutes ago/);
    expect(chip.tagName).toBe("TIME");
    expect(chip.getAttribute("dateTime")).toBe("2026-01-01T11:55:00.000Z");
    expect(chip.getAttribute("title")).toBe("2026-01-01T11:55:00.000Z");
  });

  it("renders an empty marker when missing", () => {
    render(<DateChip at={null} />);
    expect(screen.getByText("—").className).toContain("empty");
  });
});
