// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ApiError } from "../api";
import { inZone, Routines } from "./Routines";

const mock = vi.hoisted(() => ({
  routines: [] as unknown[],
  detail: {} as Record<string, unknown>,
  listError: undefined as Error | undefined,
  detailFails: false,
  patch: vi.fn(),
  post: vi.fn(),
  reload: vi.fn(),
}));

vi.mock("../api", async (orig) => ({
  ...(await orig<typeof import("../api")>()),
  api: {
    GET: (path: string, opts?: { params?: { path?: { routine_id?: string } } }) => ({
      path,
      id: opts?.params?.path?.routine_id,
    }),
    PATCH: (_path: string, opts: unknown) => ({ patch: opts }),
    POST: (_path: string, opts: unknown) => ({ post: opts }),
  },
  call: async (req: { path?: string; id?: string; patch?: unknown; post?: unknown }) => {
    if (req.patch) return mock.patch(req.patch);
    if (req.post) return mock.post(req.post);
    if (req.path === "/api/projects") return [{ slug: "radar", name: "Radar" }];
    if (req.path === "/api/projects/{slug}/routines") {
      if (mock.listError) throw mock.listError;
      return mock.routines;
    }
    if (mock.detailFails) throw new Error("detail down");
    return mock.detail[req.id ?? ""];
  },
}));

vi.mock("../live", async () => {
  const { useCallback, useEffect, useState } = await import("react");
  return {
    useLiveData: <T,>(load: (s: AbortSignal) => Promise<T>, deps: unknown[]) => {
      const [data, setData] = useState<T>();
      const [error, setError] = useState<Error>();
      const [nonce, setNonce] = useState(0);
      useEffect(() => {
        load(new AbortController().signal).then(setData, setError);
      }, [...deps, nonce]);
      const reload = useCallback(() => {
        mock.reload();
        setError(undefined);
        setNonce((n) => n + 1);
      }, []);
      return { data, error, reload };
    },
  };
});

const routine = (over: Record<string, unknown> = {}) => ({
  id: "r1",
  project: "radar",
  slug: "error-log",
  title: "Error log",
  schedule: "@hourly",
  timezone: "America/Sao_Paulo",
  template: {},
  findings: "approval",
  enabled: true,
  next_run_at: "2026-01-01T15:00:00Z",
  last_run_at: "2026-01-01T14:00:00Z",
  last_task_id: "abcdef1234567",
  created_by: "admin",
  created_at: "2025-12-01T00:00:00Z",
  updated_at: "2025-12-01T00:00:00Z",
  next_slots: ["2026-01-01T15:00:00Z"],
  last_runs: [],
  ...over,
});

function setup(list = routine(), detail: Record<string, unknown> = {}) {
  mock.routines = [list];
  mock.detail = {
    r1: {
      ...list,
      last_runs: [
        {
          task_id: "abcdef1234567",
          state: "failed",
          created_at: "2026-01-01T14:00:00Z",
          findings_filed: 2,
        },
      ],
      ...detail,
    },
  };
}

beforeEach(() => {
  mock.listError = undefined;
  mock.detailFails = false;
  mock.patch.mockReset().mockResolvedValue({});
  mock.post.mockReset().mockResolvedValue({ routine_id: "r1", task_id: "t9", skipped: null });
  mock.reload.mockReset();
});
afterEach(cleanup);

describe("Routines states", () => {
  it("shows loading before data arrives", () => {
    setup();
    render(<Routines />);
    expect(screen.getByText("Loading…")).toBeTruthy();
  });

  it("shows an empty state", async () => {
    mock.routines = [];
    render(<Routines />);
    expect(await screen.findByText("No routines in this project.")).toBeTruthy();
  });

  it("shows the error and retries", async () => {
    setup();
    mock.listError = new Error("boom");
    render(<Routines />);
    expect(await screen.findByText(/Could not load routines: boom/)).toBeTruthy();
    mock.listError = undefined;
    fireEvent.click(screen.getByRole("button", { name: "Retry" }));
    expect(await screen.findByText("Error log")).toBeTruthy();
  });
});

describe("Routines rendering", () => {
  it("shows schedule, timezone, last run task link and its state", async () => {
    setup();
    render(<Routines />);
    expect(await screen.findByText("@hourly")).toBeTruthy();
    expect(screen.getAllByText(/America\/Sao_Paulo/).length).toBeGreaterThan(0);
    const link = screen.getByRole("link", { name: "task abcdef12" });
    expect(link.getAttribute("href")).toBe("#tasks/abcdef1234567");
    expect(screen.getByText("failed")).toBeTruthy();
    expect(screen.getByText("enabled")).toBeTruthy();
    expect(screen.getByRole("button", { name: "Disable" })).toBeTruthy();
  });

  it("renders a disabled routine with no next run", async () => {
    setup(routine({ enabled: false, next_run_at: null }));
    render(<Routines />);
    expect(await screen.findByText("none while disabled")).toBeTruthy();
    expect(screen.getByText("disabled")).toBeTruthy();
    expect(screen.getByRole("button", { name: "Enable" })).toBeTruthy();
    expect((screen.getByRole("button", { name: "Run now" }) as HTMLButtonElement).disabled).toBe(
      false,
    );
  });

  it("shows the last run's findings filed as a number, including zero", async () => {
    setup();
    render(<Routines />);
    expect(await screen.findByText("2 filed")).toBeTruthy();
    cleanup();
    setup(routine(), {
      last_runs: [
        {
          task_id: "abcdef1234567",
          state: "succeeded",
          created_at: "2026-01-01T14:00:00Z",
          findings_filed: 0,
        },
      ],
    });
    render(<Routines />);
    expect(await screen.findByText("0 filed")).toBeTruthy();
  });

  it("shows unknown, never 0, when the count is missing or the detail failed", async () => {
    setup(routine(), { last_runs: [] });
    render(<Routines />);
    expect(await screen.findByText("unknown (not reported)")).toBeTruthy();
    expect(screen.queryByText(/\b0 filed/)).toBeNull();
    cleanup();
    setup();
    mock.detailFails = true;
    render(<Routines />);
    expect(await screen.findByText("unknown (not reported)")).toBeTruthy();
    expect(screen.queryByText(/\b0 filed/)).toBeNull();
  });

  it("shows a routine that never ran, and unknown for a missing run state", async () => {
    setup(routine({ last_run_at: null, last_task_id: null }));
    render(<Routines />);
    expect(await screen.findByText("not run yet")).toBeTruthy();
    expect(screen.getByText("no runs yet")).toBeTruthy();
    cleanup();
    setup(routine(), { last_runs: [] });
    render(<Routines />);
    expect(await screen.findByText("state unknown")).toBeTruthy();
  });

  it("falls back to the listed routine when its detail cannot be read", async () => {
    setup();
    mock.detailFails = true;
    render(<Routines />);
    expect(await screen.findByText("state unknown")).toBeTruthy();
    expect(screen.getByText("Error log")).toBeTruthy();
  });

  it("formats times in the routine timezone", () => {
    expect(inZone("2026-01-01T15:00:00Z", "America/Sao_Paulo")).toContain("12:00");
    expect(inZone("2026-01-01T15:00:00Z", "Not/AZone")).toMatch(/15:00 UTC$/);
  });
});

describe("Routines actions", () => {
  it("disables a routine with PATCH and reloads", async () => {
    setup();
    render(<Routines />);
    fireEvent.click(await screen.findByRole("button", { name: "Disable" }));
    await waitFor(() => expect(mock.reload).toHaveBeenCalled());
    expect(mock.patch).toHaveBeenCalledWith({
      params: { path: { slug: "radar", routine: "error-log" } },
      body: { enabled: false },
    });
  });

  it("run now shows a pending state, then the started task", async () => {
    setup();
    let finish: (v: unknown) => void = () => {};
    mock.post.mockReturnValue(new Promise((r) => (finish = r)));
    render(<Routines />);
    fireEvent.click(await screen.findByRole("button", { name: "Run now" }));
    const pending = await screen.findByRole("button", { name: "Starting…" });
    expect((pending as HTMLButtonElement).disabled).toBe(true);
    finish({ routine_id: "r1", task_id: "t9", skipped: null });
    expect(await screen.findByText("Run started as task t9.")).toBeTruthy();
    expect(mock.reload).toHaveBeenCalled();
  });

  it("run now skipped by overlap is a distinct notice, not an error", async () => {
    setup();
    mock.post.mockResolvedValue({ routine_id: "r1", task_id: null, skipped: "previous_running" });
    render(<Routines />);
    fireEvent.click(await screen.findByRole("button", { name: "Run now" }));
    const note = await screen.findByText(/Already running, not started/);
    expect(note.getAttribute("role")).toBe("status");
    expect(screen.queryByText(/Run started/)).toBeNull();
  });

  it("shows the API error for a forbidden action", async () => {
    setup();
    mock.post.mockRejectedValue(new ApiError(403, "forbidden", "manager scope required"));
    render(<Routines />);
    fireEvent.click(await screen.findByRole("button", { name: "Run now" }));
    const alert = await screen.findByRole("alert");
    expect(alert.textContent).toBe("Not permitted (403): manager scope required");
    expect((screen.getByRole("button", { name: "Run now" }) as HTMLButtonElement).disabled).toBe(
      false,
    );
  });
});
