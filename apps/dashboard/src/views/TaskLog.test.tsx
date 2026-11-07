// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { TaskLog } from "./TaskLog";

type Page = { attempt: number; lines: unknown[]; last_seq: number };
const mock = vi.hoisted(() => ({
  page: { attempt: 2, lines: [], last_seq: 0 } as Page,
  after: { attempt: 2, lines: [], last_seq: 0 } as Page, // the answer to `after` reads
  query: undefined as unknown,
  queries: [] as { after?: number }[],
  connection: 1,
  listener: undefined as undefined | ((e: unknown) => void),
}));

vi.mock("../api", () => ({
  api: {
    GET: (path: string, opts: { params: { query: { after?: number } } }) => {
      mock.query ??= opts.params.query;
      mock.queries.push(opts.params.query);
      return { path, after: opts.params.query.after };
    },
  },
  call: async (req: { after?: number }) => (req.after == null ? mock.page : mock.after),
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
  // `subscribe` is stable, like the real context value.
  return { useLive: () => ({ ...live, connection: mock.connection }) };
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
  mock.after = { attempt: 2, lines: [], last_seq: 0 };
  mock.query = undefined;
  mock.queries = [];
  mock.connection = 1;
});
afterEach(cleanup);

describe("TaskLog", () => {
  it("backfills the attempt, then follows live events without duplicates", async () => {
    render(<TaskLog taskId="t1" attempt={2} />);
    await screen.findByText("first");
    expect(mock.query).toEqual({ attempt: 2 });
    expect(screen.getByText(/attempt 2/)).toBeTruthy();
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
    fireEvent.change(screen.getByLabelText("Source"), { target: { value: "delegate" } });
    expect(screen.queryByText("first")).toBeNull();
    expect(screen.getByText(/from delegate/)).toBeTruthy();
    fireEvent.change(screen.getByLabelText("Source"), { target: { value: "worker" } });
    expect(screen.getByText("first")).toBeTruthy();
    expect(screen.queryByText(/from delegate/)).toBeNull();
  });

  it("pauses the view while lines keep arriving, then resumes", async () => {
    render(<TaskLog taskId="t1" attempt={2} />);
    await screen.findByText("first");
    fireEvent.click(screen.getByRole("button", { name: "Pause" }));
    emit("t1", 2, 2, ["while paused"]);
    expect(screen.queryByText("while paused")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Resume (+1)" }));
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

  it("reads back what it missed after the stream reconnects", async () => {
    const { rerender } = render(<TaskLog taskId="t1" attempt={2} />);
    await screen.findByText("first");
    expect(mock.queries).toHaveLength(1); // no catch-up without a reconnect
    mock.after = {
      attempt: 2,
      lines: [{ seq: 2, source: "worker", at, line: "missed" }],
      last_seq: 2,
    };
    mock.connection = 2;
    rerender(<TaskLog taskId="t1" attempt={2} />);
    await screen.findByText("missed");
    expect(mock.queries.at(-1)).toEqual({ attempt: 2, after: 1, limit: 1000 });
  });

  it("reads back the lines of a seq gap", async () => {
    render(<TaskLog taskId="t1" attempt={2} />);
    await screen.findByText("first");
    mock.after = {
      attempt: 2,
      lines: [
        { seq: 2, source: "worker", at, line: "gap a" },
        { seq: 3, source: "delegate", at, line: "gap b" },
      ],
      last_seq: 3,
    };
    emit("t1", 2, 4, ["after the gap"]);
    await screen.findByText("gap a");
    expect(mock.queries.at(-1)).toEqual({ attempt: 2, after: 1, limit: 1000 });
    const shown = [...document.querySelectorAll(".log-line")].map((el) => el.textContent);
    expect(shown).toEqual(["first", "gap a", "[delegate] gap b", "after the gap"]);
    emit("t1", 2, 5, ["contiguous"]);
    expect(mock.queries).toHaveLength(2); // no gap, no read
  });

  it("reads back a gap before the first live line when the tail was empty", async () => {
    mock.page = { attempt: 2, lines: [], last_seq: 0 };
    render(<TaskLog taskId="t1" attempt={2} />);
    await screen.findByText("No output yet.");
    mock.after = {
      attempt: 2,
      lines: [{ seq: 1, source: "worker", at, line: "missed first" }],
      last_seq: 1,
    };
    emit("t1", 2, 2, ["live"]);
    await screen.findByText("missed first");
    expect(mock.queries.at(-1)).toEqual({ attempt: 2, after: 0, limit: 1000 });
    const shown = [...document.querySelectorAll(".log-line")].map((el) => el.textContent);
    expect(shown).toEqual(["missed first", "live"]);
  });

  it("an empty tail of a later attempt starts after the task's log_seq: no needless read", async () => {
    // seq counts across attempts: attempt 1 used seq 1..40, so attempt 2 starts at 41
    mock.page = { attempt: 2, lines: [], last_seq: 40 };
    render(<TaskLog taskId="t1" attempt={2} />);
    await screen.findByText("No output yet.");
    emit("t1", 2, 41, ["first of attempt 2"]);
    await screen.findByText("first of attempt 2");
    expect(mock.queries).toHaveLength(1); // the tail only
    emit("t1", 2, 43, ["after a gap"]);
    await waitFor(() =>
      expect(mock.queries.at(-1)).toEqual({ attempt: 2, after: 41, limit: 1000 }),
    );
  });

  describe("download", () => {
    const blobs: Blob[] = [];
    const clicked: string[] = [];
    const revoked: string[] = [];

    beforeEach(() => {
      blobs.length = 0;
      clicked.length = 0;
      revoked.length = 0;
      URL.createObjectURL = (b: Blob | MediaSource) => {
        blobs.push(b as Blob);
        return "blob:log";
      };
      URL.revokeObjectURL = (u: string) => {
        revoked.push(u);
      };
      vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(function (
        this: HTMLAnchorElement,
      ) {
        clicked.push(this.download);
      });
    });
    afterEach(() => vi.restoreAllMocks());

    const button = () => screen.getByRole("button", { name: "Download visible output" });

    it("is disabled while no row is shown, and makes no download", async () => {
      mock.page = { attempt: 2, lines: [], last_seq: 0 };
      render(<TaskLog taskId="t1" attempt={2} />);
      await screen.findByText("No output yet.");
      expect((button() as HTMLButtonElement).disabled).toBe(true);
      fireEvent.click(button());
      expect(blobs).toHaveLength(0);
    });

    it("saves the visible rows as a loaded excerpt and revokes the URL", async () => {
      vi.useFakeTimers({ toFake: ["setTimeout"] });
      try {
        render(<TaskLog taskId="t/1" attempt={2} />);
        await vi.waitFor(() => expect(screen.getByText("first")).toBeTruthy());
        emit("t/1", 2, 2, ["from delegate"], "delegate");
        fireEvent.click(screen.getByRole("button", { name: "Pause" }));
        emit("t/1", 2, 3, ["hidden while paused"]);
        fireEvent.click(button());
        expect((blobs[0] as Blob).type).toBe("text/plain;charset=utf-8");
        expect(await (blobs[0] as Blob).text()).toBe(
          [
            "# Argus task log: loaded excerpt, not a full transcript",
            "# task: t/1",
            "# attempt: 2",
            "# source: all",
            "# cutoff: paused after seq 2",
            "# lines: 2",
            "",
            "first",
            "[delegate] from delegate",
            "",
          ].join("\n"),
        );
        expect(clicked).toEqual(["argus-task-t-1-attempt-2-all.txt"]);
        expect(document.querySelector("a[download]")).toBeNull();
        expect(revoked).toEqual([]);
        vi.runAllTimers();
        expect(revoked).toEqual(["blob:log"]);
      } finally {
        vi.useRealTimers();
      }
    });

    it("follows the source filter, and an attempt switch carries no old rows", async () => {
      const { rerender } = render(<TaskLog taskId="t1" attempt={2} />);
      await screen.findByText("first");
      emit("t1", 2, 2, ["from delegate"], "delegate");
      fireEvent.change(screen.getByLabelText("Source"), { target: { value: "delegate" } });
      fireEvent.click(button());
      const text = await (blobs[0] as Blob).text();
      expect(text).toContain("# source: delegate\n");
      expect(text.endsWith("\n\n[delegate] from delegate\n")).toBe(true);
      expect(clicked.at(-1)).toBe("argus-task-t1-attempt-2-delegate.txt");

      fireEvent.change(screen.getByLabelText("Source"), { target: { value: "all" } });
      mock.page = {
        attempt: 3,
        lines: [{ seq: 9, source: "worker", at, line: "new" }],
        last_seq: 9,
      };
      rerender(<TaskLog taskId="t1" attempt={3} />);
      await screen.findByText("new");
      fireEvent.click(button());
      const next = await (blobs[1] as Blob).text();
      expect(next).toContain("# attempt: 3\n");
      expect(next).not.toContain("first");
      expect(next).not.toContain("from delegate");
      expect(clicked.at(-1)).toBe("argus-task-t1-attempt-3-all.txt");
    });
  });
});
