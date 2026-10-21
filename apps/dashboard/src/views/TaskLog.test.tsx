// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { TaskLog } from "./TaskLog";

const mock = vi.hoisted(() => ({
  page: { attempt: 2, lines: [] as unknown[], last_seq: 0 },
  query: undefined as unknown,
  listener: undefined as undefined | ((e: unknown) => void),
}));

vi.mock("../api", () => ({
  api: {
    GET: (path: string, opts: { params: { query: unknown } }) => {
      mock.query = opts.params.query;
      return { path };
    },
  },
  call: async () => mock.page,
}));

vi.mock("../live", () => {
  const live = {
    subscribe: (fn: (e: unknown) => void) => {
      mock.listener = fn;
      return () => {
        mock.listener = undefined;
      };
    },
  };
  return { useLive: () => live }; // stable, like the real context value
});

const at = "2026-10-19T12:00:00Z";
const emit = (task_id: string, attempt: number, seq: number, lines: string[], source = "worker") =>
  act(() =>
    mock.listener?.({ kind: "task.log", data: { task_id, attempt, source, seq, at, lines } }),
  );

beforeEach(() => {
  mock.page = {
    attempt: 2,
    lines: [{ seq: 1, source: "worker", at, line: "first" }],
    last_seq: 1,
  };
});
afterEach(cleanup);

describe("TaskLog", () => {
  it("backfills the attempt, then follows live events without duplicates", async () => {
    render(<TaskLog taskId="t1" attempt={2} />);
    await screen.findByText("first");
    expect(mock.query).toEqual({ attempt: 2 });
    expect(screen.getByText("tentativa 2")).toBeTruthy();
    emit("t1", 2, 1, ["first", "second"]);
    expect(screen.getAllByText("first")).toHaveLength(1);
    expect(screen.getByText("second")).toBeTruthy();
    emit("other", 2, 3, ["foreign"]);
    emit("t1", 1, 3, ["old attempt"]);
    expect(screen.queryByText("foreign")).toBeNull();
    expect(screen.queryByText("old attempt")).toBeNull();
  });

  it("filters by source", async () => {
    render(<TaskLog taskId="t1" attempt={2} />);
    await screen.findByText("first");
    emit("t1", 2, 2, ["from delegate"], "delegate");
    fireEvent.change(screen.getByLabelText("Origem"), { target: { value: "delegate" } });
    expect(screen.queryByText("first")).toBeNull();
    expect(screen.getByText(/from delegate/)).toBeTruthy();
    fireEvent.change(screen.getByLabelText("Origem"), { target: { value: "worker" } });
    expect(screen.getByText("first")).toBeTruthy();
    expect(screen.queryByText(/from delegate/)).toBeNull();
  });

  it("pauses the view while lines keep arriving, then resumes", async () => {
    render(<TaskLog taskId="t1" attempt={2} />);
    await screen.findByText("first");
    fireEvent.click(screen.getByRole("button", { name: "Pausar" }));
    emit("t1", 2, 2, ["while paused"]);
    expect(screen.queryByText("while paused")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Retomar (+1)" }));
    await waitFor(() => expect(screen.getByText("while paused")).toBeTruthy());
  });

  it("keeps events that arrive before the backfill", async () => {
    render(<TaskLog taskId="t1" attempt={2} />);
    emit("t1", 2, 2, ["early"]);
    await screen.findByText("first");
    expect(screen.getByText("early")).toBeTruthy();
  });

  it("scrolls to the newest line while following", async () => {
    render(<TaskLog taskId="t1" attempt={2} />);
    await screen.findByText("first");
    const box = document.querySelector(".log") as HTMLElement;
    Object.defineProperty(box, "scrollHeight", { configurable: true, value: 500 });
    emit("t1", 2, 2, ["more"]);
    expect(box.scrollTop).toBe(500);
  });

  it("stops following once the reader scrolls up", async () => {
    render(<TaskLog taskId="t1" attempt={2} />);
    await screen.findByText("first");
    const box = document.querySelector(".log") as HTMLElement;
    Object.defineProperty(box, "scrollHeight", { configurable: true, value: 500 });
    Object.defineProperty(box, "clientHeight", { configurable: true, value: 100 });
    box.scrollTop = 50;
    fireEvent.scroll(box);
    emit("t1", 2, 2, ["more"]);
    expect(box.scrollTop).toBe(50);
    box.scrollTop = 400;
    fireEvent.scroll(box);
    emit("t1", 2, 3, ["again"]);
    expect(box.scrollTop).toBe(500);
  });
});
