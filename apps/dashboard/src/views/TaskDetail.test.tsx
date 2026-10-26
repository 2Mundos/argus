// @vitest-environment jsdom
import { act, cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { download } from "../api";
import { TaskDetail } from "./TaskDetail";

const mock = vi.hoisted(() => ({
  task: {} as Record<string, unknown>,
  trace: {} as Record<string, unknown>,
  artifacts: [] as Record<string, unknown>[],
}));
vi.mock("../api", () => ({
  api: { GET: (path: string) => path },
  call: async (path: string) =>
    path.includes("/models")
      ? mock.trace
      : path.includes("delivery")
        ? null
        : path.includes("artifacts")
          ? mock.artifacts
          : path.includes("events")
            ? []
            : mock.task,
  download: vi.fn(),
}));
vi.mock("../live", async () => {
  const React = await import("react");
  return {
    useLiveData: (load: () => Promise<unknown>) => {
      const [data, setData] = React.useState<unknown>();
      React.useEffect(() => {
        load().then(setData);
      }, []);
      return { data, reload: () => {} };
    },
  };
});
vi.mock("./Activity", () => ({ Activity: () => null }));
vi.mock("./AgentDetail", () => ({ AgentDetail: () => null }));
vi.mock("./Events", () => ({ EventList: () => null }));
vi.mock("./TaskEditForm", () => ({ TaskEditForm: () => null }));
vi.mock("./TaskLog", () => ({ TaskLog: () => null }));
vi.mock("../delivery", () => ({ TaskDelivery: () => null }));
vi.mock("../failover", () => ({ TaskFailover: () => null }));

const base = {
  id: "t1",
  title: "trace task",
  project: "global",
  state: "succeeded",
  kind: "generic",
  difficulty: "standard",
  difficulty_source: "heuristic",
  knowledge: [],
  attempt: 2,
  failures: 1,
  max_attempts: 3,
  required_capabilities: [],
  depends_on: [],
  created_at: "2026-10-20T12:00:00Z",
  created_by: "test",
  editable_fields: [],
  spec: "",
  result: null,
};

afterEach(() => {
  cleanup();
  mock.artifacts = [];
});
describe("TaskDetail model trace", () => {
  it("renders attempt grouped harness and delegate executions, evidence, accounts, usage and switches", async () => {
    mock.task = base;
    mock.trace = {
      executions: [
        {
          id: "h",
          attempt: 2,
          layer: "harness",
          engine: "codex",
          state: "succeeded",
          observed_model: "gpt-6",
          evidence: "cli_output",
          account_name: "Harness acct",
          usage: { input_tokens: 8, output_tokens: 3, cost_usd: 0.12 },
        },
        {
          id: "d",
          attempt: 2,
          layer: "delegate",
          executor: "claude",
          state: "failed",
          configured_model: "opus",
          evidence: "not_reported",
          account_name: "Delegate acct",
        },
        {
          id: "unknown",
          attempt: 1,
          layer: "delegate",
          executor: "codex",
          state: "failed",
          evidence: "not_reported",
        },
      ],
      timeline: [
        {
          id: "s",
          attempt: 2,
          kind: "model_switch",
          from_model: "sonnet",
          to_model: "opus",
          reason_code: "verification_failed",
        },
      ],
    };
    await act(async () => {
      render(<TaskDetail taskId="t1" onClose={() => {}} />);
    });
    expect(screen.getByRole("region", { name: "Attempt 2 models" })).toBeTruthy();
    const attemptTwo = screen.getByRole("region", { name: "Attempt 2 models" });
    expect(attemptTwo.querySelectorAll("p")[0]?.textContent).toContain(
      "codex gpt-6 (cli_output) · account Harness acct",
    );
    expect(attemptTwo.querySelectorAll("p")[1]?.textContent).toContain(
      "claude opus (configured) · account Delegate acct",
    );
    expect(screen.getByText(/8\+3 tokens · \$0.12/)).toBeTruthy();
    expect(screen.getByText(/model_switch · verification_failed · sonnet → opus/)).toBeTruthy();
    expect(screen.getByText(/not reported/)).toBeTruthy();
  });
});

describe("TaskDetail artifacts", () => {
  it("shows name, size, media type and source, and downloads from the content path", async () => {
    mock.task = base;
    mock.trace = { executions: [], timeline: [] };
    mock.artifacts = [
      {
        id: "a1",
        attempt: 0,
        name: "shot.png",
        size: 2048,
        media_type: "image/png",
        source: "attachment",
      },
    ];
    await act(async () => {
      render(<TaskDetail taskId="t1" onClose={() => {}} />);
    });
    const item = screen.getByText("shot.png").closest("li");
    expect(item?.textContent).toContain("2.0 KB");
    expect(item?.textContent).toContain("image/png");
    expect(item?.textContent).toContain("attachment");
    await act(async () => {
      screen.getByText("shot.png").click();
    });
    expect(download).toHaveBeenCalledWith("/api/artifacts/a1/content", "shot.png");
  });
});
