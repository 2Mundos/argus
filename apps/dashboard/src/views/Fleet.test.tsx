// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { Fleet, percent, seconds } from "./Fleet";

const mock = vi.hoisted(() => ({
  health: undefined as unknown,
  cleanup: undefined as unknown,
  posts: [] as { path: string; body: unknown }[],
}));

vi.mock("../api", () => ({
  api: {
    GET: (path: string) => path,
    POST: (path: string, init?: { body?: unknown }) => {
      mock.posts.push({ path, body: init?.body });
      return path;
    },
  },
  call: async (path: string) => {
    if (!path.startsWith("/api/fleet/workspace-cleanup")) return mock.health;
    if (mock.cleanup === undefined) throw new Error("403 forbidden");
    return mock.cleanup;
  },
}));

vi.mock("../live", async () => {
  const { useEffect, useState } = await import("react");
  return {
    useLiveData: <T,>(load: () => Promise<T>) => {
      const [data, setData] = useState<T>();
      useEffect(() => {
        load().then(setData);
      }, []);
      return { data, error: undefined, reload: () => {} };
    },
  };
});

afterEach(() => {
  cleanup();
  mock.cleanup = undefined;
  mock.posts = [];
});

const durations = (n: number, p: number | null) => ({
  window_hours: 24,
  source: "events task.claim",
  n,
  p50: p,
  p90: p,
  mean: p,
});

function health(over: Record<string, unknown> = {}) {
  return {
    as_of: "2026-03-01T00:00:00Z",
    window_hours: 24,
    truncated: false,
    pools: [
      {
        name: "alpha",
        status: "ok",
        desired: 2,
        live: 1,
        running: 1,
        last_heartbeat_at: null,
        source: "agents.last_heartbeat_at",
      },
    ],
    tasks_by_state: { source: "tasks", counts: { ready: 0, failed: 3 } },
    queue_time: durations(2, null),
    run_time: durations(6, 90),
    outcomes: {
      window_hours: 24,
      source: "tasks",
      succeeded: 1,
      failed: 1,
      total: 2,
      success_rate: null,
      failure_rate: null,
    },
    failures: {
      window_hours: 24,
      source: "events task.fail, task.expire",
      total: 1,
      truncated: false,
      groups: [
        {
          reason: "auth_missing",
          count: 1,
          examples: [
            {
              task_id: "t-9",
              title: "Build it",
              error: "auth_missing: login",
              at: "2026-02-28T23:00:00Z",
            },
          ],
        },
      ],
    },
    yield: {
      window_hours: 24,
      source: "events; tasks; account_usage",
      attempts: 6,
      completions: 2,
      attempt_success_rate: 0.33,
      finished_tasks: 2,
      attempts_per_finished_task: null,
      usage_tasks: 0,
      tokens_per_succeeded: null,
      tokens_per_failed: null,
      cost_per_succeeded: null,
    },
    ...over,
  };
}

function cleanupSummary() {
  return {
    settings: {
      enabled: true,
      dry_run: true,
      archive_root: null,
      min_age_hours: 72,
      interval_s: 3600,
      low_disk_bytes: 0,
      max_archive_bytes: 0,
      max_attempts: 3,
      max_instances: 100,
      run_timeout_s: 600,
    },
    running: false,
    last_run: null,
    counts: { eligible: 2, preserved: 5 },
    bytes_estimated: 2048,
    bytes_freed: 0,
    problems: [],
  };
}

describe("Fleet", () => {
  it("renders no data for unknown values, never 0", async () => {
    mock.health = health();
    render(<Fleet />);
    await screen.findByText("alpha");
    const pools = screen.getAllByRole("table")[0];
    if (!pools) throw new Error("no pools table");
    expect(within(pools).getByText("1 / 2")).toBeTruthy();
    expect(within(pools).getByText("no data")).toBeTruthy(); // heartbeat
    const queue = screen.getByRole("heading", { name: "Queue time" }).parentElement;
    expect(queue?.textContent).toContain("p50 no data");
    expect(queue?.textContent).toContain("n=2");
    const outcomes = screen.getByRole("heading", { name: "Outcomes" }).parentElement;
    expect(outcomes?.textContent).toContain("success no data");
    expect(outcomes?.textContent).toContain("1 succeeded · 1 failed");
    expect(outcomes?.textContent).not.toMatch(/\b0%/);
  });

  it("shows values with window and source captions, and links failures", async () => {
    mock.health = health();
    render(<Fleet />);
    const run = (await screen.findByRole("heading", { name: "Run time" })).parentElement;
    expect(run?.textContent).toContain("p50 1.5 min");
    expect(run?.textContent).toContain("24 h · events task.claim");
    expect(screen.getByText("24 h · events task.fail, task.expire")).toBeTruthy();
    const link = screen.getByRole("link", { name: "Build it" });
    expect(link.getAttribute("href")).toBe("#tasks/t-9");
    expect(screen.getByText("auth_missing: login")).toBeTruthy();
  });

  it("shows yield with its usage coverage, no data when unknown", async () => {
    mock.health = health();
    render(<Fleet />);
    const tokens = (await screen.findByRole("heading", { name: "Tokens per task" })).parentElement;
    expect(tokens?.textContent).toContain("succeeded no data");
    expect(tokens?.textContent).toContain("usage reported for 0 of 2 finished tasks");
    const attempts = screen.getByRole("heading", { name: "Attempts" }).parentElement;
    expect(attempts?.textContent).toContain("6 claimed");
    expect(attempts?.textContent).toContain("success per attempt 33%");
  });

  it("shows rates when known", async () => {
    mock.health = health({
      outcomes: {
        window_hours: 24,
        source: "tasks",
        succeeded: 4,
        failed: 1,
        total: 5,
        success_rate: 0.8,
        failure_rate: 0.2,
      },
    });
    render(<Fleet />);
    const o = (await screen.findByRole("heading", { name: "Outcomes" })).parentElement;
    expect(o?.textContent).toContain("success 80%");
  });

  it("shows the workspace cleanup card and starts a dry run", async () => {
    mock.health = health();
    mock.cleanup = cleanupSummary();
    render(<Fleet />);
    const card = (await screen.findByRole("heading", { name: "Workspace cleanup" })).parentElement;
    expect(card?.textContent).toContain("enabled · dry-run mode");
    expect(card?.textContent).toContain("archive root not set");
    expect(card?.textContent).toContain("last run no data");
    expect(card?.textContent).toContain("2 eligible · 5 preserved");
    expect(card?.textContent).toContain("approx. 2.0 KB estimated · 0 B freed");
    fireEvent.click(screen.getByRole("button", { name: "Dry run now" }));
    await waitFor(() =>
      expect(mock.posts).toEqual([
        { path: "/api/fleet/workspace-cleanup/run", body: { dry_run: true } },
      ]),
    );
  });

  it("hides the workspace cleanup card when the summary is unavailable", async () => {
    mock.health = health();
    render(<Fleet />);
    await screen.findByText("alpha");
    expect(screen.queryByRole("heading", { name: "Workspace cleanup" })).toBeNull();
  });

  it("formats helpers", () => {
    expect(seconds(null)).toBe("no data");
    expect(seconds(12.4)).toBe("12 s");
    expect(seconds(7200)).toBe("2.0 h");
    expect(percent(undefined)).toBe("no data");
    expect(percent(0)).toBe("0%"); // a measured zero is not missing
  });
});
