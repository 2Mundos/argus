// @vitest-environment jsdom
import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import type { ArgusEvent } from "../api";
import { EventList } from "./EventList";

afterEach(cleanup);

describe("EventList", () => {
  it("shows the rule id and reason of a blocked command", () => {
    const events: ArgusEvent[] = [
      {
        id: 1,
        at: "2026-01-01T00:00:00Z",
        actor: "orchestrator",
        kind: "task.command_blocked",
        subject_type: "task",
        subject_id: "task-abcd1234",
        data: { tool: "Bash", rule: "rm_root", reason: "recursive delete of the filesystem root" },
      },
    ];
    render(<EventList events={events} />);
    expect(screen.getByText(/rm_root · recursive delete of the filesystem root/)).toBeTruthy();
    expect(screen.getByText("command_blocked").className).toContain("state-blocked");
  });
});
