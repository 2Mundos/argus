/**
 * A preflight session's gateway lease must not disturb the agent group's other state
 * (docs/specs/failure-classes.spec.md 3.3.1). Runs the REAL OneCLI adapter (upstream's
 * add-onecli skill plus the Argus hooks) against a stubbed SDK and fetch: ensuring and
 * releasing a preflight lease only reads, and leaves a live worker session's lease (health
 * monitor, controller, availability reports) and all OneCLI assignments alone.
 */
import { afterEach, beforeEach, describe, expect, it, type MockInstance, vi } from "vitest";

vi.mock("../db/agent-groups.js", async (original) => ({
  ...(await original<typeof import("../db/agent-groups.js")>()),
  getAgentGroup: async () => undefined,
}));

const sdk = vi.hoisted(() => ({
  ensureAgent: vi.fn(async () => ({ created: false })),
  getContainerConfig: vi.fn(async () => ({
    env: { HTTPS_PROXY: "http://host.docker.internal:15001" },
    caCertificate: "synthetic-ca",
    caCertificateContainerPath: "/tmp/onecli-ca.pem",
  })),
}));

vi.mock("@onecli-sh/sdk", () => ({
  OneCLI: class {
    ensureAgent = sdk.ensureAgent;
    getContainerConfig = sdk.getContainerConfig;
  },
  ApprovalClient: class {
    start() {
      return new Promise<void>(() => undefined);
    }
    stop() {}
  },
}));
vi.mock("../config.js", async (original) => ({
  ...(await original<typeof import("../config.js")>()),
  DATA_DIR: "/tmp/nanoclaw-preflight-lease-review",
}));
vi.mock("../log.js", () => ({
  log: { debug: vi.fn(), info: vi.fn(), warn: vi.fn(), error: vi.fn(), fatal: vi.fn() },
}));
vi.mock("../env.js", () => ({
  readEnvFile: () => ({ ONECLI_URL: "http://localhost:1", ONECLI_API_KEY: "unused" }),
}));

import { releaseGatewaySession } from "../gateway-session-lifecycle.js";
import { getGatewayProviderRegistration } from "./gateway-provider-registry.js";
import "./onecli.js";

const provider = getGatewayProviderRegistration("onecli")!;
const input = (sessionId: string) => ({
  key: { installSlug: "install", agentGroupId: "g1", sessionId },
  runtimeIdentity: `install/g1/${sessionId}`,
  groupName: "Group One",
  containerName: `c-${sessionId}`,
  capabilities: {} as never,
});

describe("preflight lease vs the group's live session", () => {
  let fetchMock: MockInstance<typeof fetch>;
  beforeEach(() => {
    vi.useFakeTimers();
    sdk.ensureAgent.mockClear();
    sdk.getContainerConfig.mockClear();
    fetchMock = vi.spyOn(globalThis, "fetch").mockImplementation(async () => Response.json([]));
  });
  afterEach(() => {
    fetchMock.mockRestore();
    vi.useRealTimers();
  });

  it("ensuring and releasing it reads only and leaves the live session's lease running", async () => {
    const workerController = new AbortController();
    const worker = await provider.sessions.ensure(input("worker"), workerController.signal);
    const unavailable = vi.fn();
    worker.onUnavailable?.(unavailable);
    expect(vi.getTimerCount()).toBe(1); // one shared health monitor
    fetchMock.mockClear();
    sdk.ensureAgent.mockClear();
    sdk.getContainerConfig.mockClear();

    // As NanoClaw's ensureGatewaySession does: one controller for the lease and its release.
    const controller = new AbortController();
    const control = {
      lease: await provider.sessions.ensure(input("preflight-1"), controller.signal),
      controller,
    };
    await releaseGatewaySession(control as never, {
      kind: "session-ended",
      reason: "repository-preflight-done",
    });

    // The adapter's only OneCLI calls: idempotent agent registration and a config read.
    expect(sdk.ensureAgent).toHaveBeenCalledTimes(1);
    expect(sdk.ensureAgent).toHaveBeenLastCalledWith({ name: "Group One", identifier: "g1" });
    expect(sdk.getContainerConfig).toHaveBeenCalledTimes(1);
    // Nothing else mutates OneCLI: no secret assignment, no release, no write of any kind.
    const mutating = fetchMock.mock.calls.filter(
      ([, init]: [unknown, RequestInit?]) => init?.method && init.method !== "GET",
    );
    expect(mutating).toEqual([]);
    // The worker's lease is untouched: not aborted, still monitored, no unavailability report.
    expect(workerController.signal.aborted).toBe(false);
    expect(unavailable).not.toHaveBeenCalled();
    expect(vi.getTimerCount()).toBe(1); // the preflight lease released; the worker's still monitored

    workerController.abort();
    expect(vi.getTimerCount()).toBe(0);
  });

  it("a gateway outage reaches the preflight lease through the same monitor", async () => {
    fetchMock.mockRejectedValue(new Error("offline"));
    const controller = new AbortController();
    const lease = await provider.sessions.ensure(input("preflight-2"), controller.signal);
    const report = vi.fn();
    lease.onUnavailable?.(report);
    await vi.advanceTimersByTimeAsync(5_000);
    expect(report).toHaveBeenCalledWith("OneCLI gateway unavailable");
    controller.abort();
  });
});
