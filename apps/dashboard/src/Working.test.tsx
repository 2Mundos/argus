// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import { ActionButton } from "./ActionButton";
import { Brand } from "./Brand";
import { pendingReply } from "./views/Chat";
import { Working } from "./Working";
import { startWorking } from "./working";

afterEach(cleanup);

const name = () => screen.getByText("Argus");

// jsdom has no CSS `animation` property, so React listens for the prefixed event name.
const endCycle = (el: Element) =>
  fireEvent(el, new Event("webkitAnimationIteration", { bubbles: true }));

describe("working signal", () => {
  it("rolls the wordmark while work is held and finishes the cycle once it ends", () => {
    render(<Brand />);
    expect(name().className).not.toContain("rolling");

    let first!: () => void;
    let second!: () => void;
    act(() => {
      first = startWorking();
      second = startWorking();
    });
    expect(name().className).toContain("rolling");

    // Releasing twice must not drop the other claim.
    act(() => {
      first();
      first();
    });
    endCycle(name());
    expect(name().className).toContain("rolling");

    act(() => second());
    // Still rolling until the cycle ends, so the gradient does not snap back.
    expect(name().className).toContain("rolling");
    endCycle(name());
    expect(name().className).not.toContain("rolling");
  });

  it("is claimed by a pending action and by a mounted Working line", () => {
    render(
      <>
        <Brand />
        <ActionButton pending>Save</ActionButton>
      </>,
    );
    expect(name().className).toContain("rolling");
    cleanup();

    const view = render(
      <>
        <Brand />
        <Working label="Working…" />
      </>,
    );
    expect(name().className).toContain("rolling");
    expect(screen.getByRole("status").textContent).toBe("Working…");
    view.rerender(<Brand />);
    endCycle(name());
    expect(name().className).not.toContain("rolling");
  });
});

describe("pendingReply", () => {
  const now = Date.parse("2026-01-01T01:00:00Z");
  const msg = (author: string, state: string, created_at = "2026-01-01T00:59:00Z") =>
    ({ author, state, created_at }) as Parameters<typeof pendingReply>[0][number];

  it.each([
    ["delivered turn", [msg("admin:a", "delivered")], false, { label: "Working…", rolling: true }],
    [
      "queued with a registered orchestrator",
      [msg("admin:a", "queued")],
      true,
      { label: "Waiting for the orchestrator…", rolling: true },
    ],
    [
      "queued with none registered",
      [msg("admin:a", "queued")],
      false,
      { label: "Queued until the orchestrator is available", rolling: false },
    ],
    ["answered", [msg("admin:a", "answered")], true, null],
    [
      "a reply came after",
      [msg("admin:a", "delivered"), msg("orchestrator:x", "posted")],
      true,
      null,
    ],
    [
      "given up after the turn timeout",
      [msg("admin:a", "delivered", "2026-01-01T00:30:00Z")],
      true,
      null,
    ],
    ["empty thread", [], true, null],
  ])("%s", (_name, items, registered, expected) => {
    expect(pendingReply(items, registered, now)).toEqual(expected);
  });
});
