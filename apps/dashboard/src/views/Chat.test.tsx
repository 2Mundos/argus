// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { Agent, Conversation, EngineCatalog, Instance, Message, Pool } from "../api";
import { LiveProvider } from "../live";
import { Chat } from "./Chat";

type Req = { method: "GET" | "POST"; path: string; opts?: { body?: unknown; params?: any } };

const net = vi.hoisted(() => ({
  onboarding: { orchestrator: true } as { orchestrator: boolean },
  conversations: [] as unknown[],
  messages: {} as Record<string, unknown[]>,
  pools: [] as unknown[],
  instances: [] as unknown[],
  agents: [] as unknown[],
  engines: undefined as unknown,
  failEngines: false,
  fail: new Set<string>(),
  posts: [] as { path: string; body: unknown }[],
  createGate: null as Promise<void> | null,
  failMessages: false,
  nextId: 1,
}));

vi.mock("../api", () => ({
  api: {
    GET: (path: string, opts?: unknown) => ({ method: "GET", path, opts }),
    POST: (path: string, opts?: unknown) => ({ method: "POST", path, opts }),
  },
  call: async (req: Req) => {
    if (req.method === "POST") {
      net.posts.push({ path: req.path, body: req.opts?.body });
      if (req.path === "/api/conversations") {
        await net.createGate;
        const id = `c${net.nextId++}`;
        const conv = { ...conversation, id, title: "New chat", kind: "chat", cost_usd: null };
        net.conversations = [conv, ...net.conversations];
        net.messages[id] = [];
        return conv;
      }
      if (net.failMessages) throw new Error("orchestrator unavailable");
      const id = req.opts?.params?.path?.conversation_id as string;
      const body = (req.opts?.body as { body: string }).body;
      net.messages[id] = [...(net.messages[id] ?? []), message(`m-${id}`, "admin:tester", body)];
      return {};
    }
    if (net.fail.has(req.path)) throw new Error(`${req.path} unavailable`);
    switch (req.path) {
      case "/api/onboarding":
        return net.onboarding;
      case "/api/conversations":
        return { items: net.conversations };
      case "/api/conversations/{conversation_id}/messages":
        return { items: net.messages[req.opts?.params?.path?.conversation_id] ?? [] };
      case "/api/pools":
        return net.pools;
      case "/api/instances":
        return net.instances;
      case "/api/agents":
        return net.agents;
      case "/api/engines":
        if (net.failEngines) throw new Error("engines unavailable");
        return net.engines;
    }
    throw new Error(`unexpected ${req.path}`);
  },
}));

// The real `useLiveData` and `LiveProvider`, with only the event stream faked, so a
// failed refetch behaves as in the app (the previous data is kept next to the error).
const stream = vi.hoisted(() => ({
  current: null as { onStatus: (s: "live") => void; onMessage: (d: string) => void } | null,
}));
vi.mock("../sse", () => ({
  followEvents: (opts: NonNullable<typeof stream.current>) => {
    stream.current = opts;
    opts.onStatus("live");
    return Promise.resolve();
  },
}));

const token = () => "tok";
const noop = () => {};
function renderChat(props: Parameters<typeof Chat>[0] = {}) {
  return render(
    <LiveProvider token={token} onUnauthorized={noop}>
      <Chat {...props} />
    </LiveProvider>,
  );
}
/** An SSE event; the provider debounces it into a refetch of every live query. */
const emit = () =>
  act(async () => stream.current?.onMessage(JSON.stringify({ kind: "task.updated" })));

const conversation = {
  id: "c0",
  kind: "chat",
  title: "Plan the release",
  title_source: "admin",
  created_by: "admin:tester",
  archived_at: null,
  session_ref: null,
  cost_usd: 1.254,
  created_at: "2026-10-29T12:00:00Z",
  updated_at: "2026-10-29T12:00:00Z",
} as Conversation;

function message(id: string, author: string, body: string): Message {
  return {
    id,
    conversation_id: "c0",
    author,
    body,
    state: author.startsWith("admin") ? "answered" : "posted",
    truncated: false,
    turn_id: null,
    delivered_at: null,
    created_at: "2026-10-29T12:01:00Z",
  };
}

const pool = (over: Partial<Pool> = {}) =>
  ({
    name: "orchestrator",
    system: "orchestrator",
    status: "ok",
    last_error: null,
    live_instances: 1,
    desired_replicas: 1,
    ...over,
  }) as Pool;
const instance = (over: Partial<Instance> = {}) =>
  ({
    id: "i1",
    pool: "orchestrator",
    state: "running",
    draining: false,
    last_error: null,
    agents: [{ id: "a1", name: "orch", claimed_tasks: 0 }],
    ...over,
  }) as Instance;
const agent = (over: Partial<Agent> = {}) =>
  ({
    id: "a1",
    name: "orch",
    role: "orchestrator",
    state: "active",
    instance_id: "i1",
    last_heartbeat_at: new Date(Date.now() - 5_000).toISOString(),
    end_reason: null,
    ...over,
  }) as Agent;
type Sums = { leasable?: number | null; busy?: number };
const layer = (layer: "harness" | "executor", accounts: Sums | null) => ({
  layer,
  usable: true,
  accounts: accounts && { available: 1, exhausted: 0, disabled: 0, busy: 0, ...accounts },
});
const catalog = (...engines: ReturnType<typeof layer>[][]) =>
  ({
    engines: engines.map((layers, i) => ({ id: `e${i}`, layers })),
    local: null,
  }) as unknown as EngineCatalog;

const messageBox = () =>
  screen.findByRole("textbox", { name: "Message" }) as Promise<HTMLTextAreaElement>;
// Thread messages only: a draft inside the textarea also matches getByText.
const findArticle = (text: string) =>
  waitFor(() => {
    const el = [...document.querySelectorAll("article")].find((a) => a.textContent?.includes(text));
    if (!el) throw new Error(`no message containing ${text}`);
    return el;
  });
const messagePosts = () => net.posts.filter((p) => p.path.endsWith("/messages"));
const convPosts = () => net.posts.filter((p) => p.path === "/api/conversations");

beforeEach(() => {
  net.onboarding = { orchestrator: true };
  net.conversations = [];
  net.messages = {};
  net.pools = [pool()];
  net.instances = [instance()];
  net.agents = [agent()];
  net.engines = catalog([layer("harness", { leasable: 1 })]);
  net.failEngines = false;
  net.fail.clear();
  stream.current = null;
  net.posts = [];
  net.createGate = null;
  net.failMessages = false;
  net.nextId = 1;
});
afterEach(cleanup);

describe("Chat when the orchestrator is off", () => {
  it("shows the empty state and opens onboarding step 3, with no composer", async () => {
    net.onboarding = { orchestrator: false };
    const open = vi.fn();
    renderChat({ onOpenSetup: open });
    expect(await screen.findByText("The orchestrator is off")).toBeTruthy();
    expect(screen.queryByRole("textbox")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Set up orchestrator" }));
    expect(open).toHaveBeenCalledWith(3);
  });
});

describe("Chat first send", () => {
  it("creates a conversation, then posts the trimmed message to it", async () => {
    renderChat();
    const box = await messageBox();
    fireEvent.change(box, { target: { value: "  Ship the fix  " } });
    fireEvent.keyDown(box, { key: "Enter" });

    expect(await findArticle("Ship the fix")).toBeTruthy();
    expect(convPosts()).toHaveLength(1);
    expect(messagePosts()).toEqual([
      {
        path: "/api/conversations/{conversation_id}/messages",
        body: { body: "Ship the fix" },
      },
    ]);
    // Author and a timestamp accompany the message.
    expect(screen.getByText(/^admin:tester ·/)).toBeTruthy();
    expect(document.querySelector("article time")).toBeTruthy();
  });

  it("does not send an empty message, whitespace, or Shift+Enter", async () => {
    renderChat();
    const box = await messageBox();
    fireEvent.keyDown(box, { key: "Enter" });
    fireEvent.change(box, { target: { value: "   " } });
    fireEvent.keyDown(box, { key: "Enter" });
    expect((screen.getByRole("button", { name: "Send" }) as HTMLButtonElement).disabled).toBe(true);
    fireEvent.change(box, { target: { value: "line one" } });
    fireEvent.keyDown(box, { key: "Enter", shiftKey: true });
    await act(async () => {});
    expect(net.posts).toEqual([]);
    expect(box.value).toBe("line one");
  });

  it("sends once when Enter is pressed twice before the first send settles", async () => {
    let release = () => {};
    net.createGate = new Promise<void>((r) => {
      release = r;
    });
    renderChat();
    const box = await messageBox();
    fireEvent.change(box, { target: { value: "only once" } });
    fireEvent.keyDown(box, { key: "Enter" });
    fireEvent.keyDown(box, { key: "Enter" });
    fireEvent.submit(box.closest("form") as HTMLFormElement);
    await act(async () => release());

    expect(await findArticle("only once")).toBeTruthy();
    expect(convPosts()).toHaveLength(1);
    expect(messagePosts()).toHaveLength(1);
  });

  it("keeps the draft on failure and allows a retry", async () => {
    net.failMessages = true;
    renderChat();
    const box = await messageBox();
    fireEvent.change(box, { target: { value: "try me" } });
    fireEvent.keyDown(box, { key: "Enter" });
    expect(await screen.findByText("orchestrator unavailable")).toBeTruthy();
    expect(box.value).toBe("try me");

    net.failMessages = false;
    await waitFor(() => expect(box.disabled).toBe(false));
    fireEvent.keyDown(box, { key: "Enter" });
    await waitFor(() => expect(messagePosts()).toHaveLength(2));
  });
});

describe("Chat thread", () => {
  beforeEach(() => {
    net.conversations = [conversation];
    net.messages.c0 = [
      message("m1", "admin:tester", "Status?"),
      message("m2", "orchestrator:agent-1", "All green."),
      message("m3", "system", "The orchestrator did not answer this turn."),
    ];
  });

  it("shows author and timestamp per message and the thread cost", async () => {
    renderChat();
    fireEvent.click(await screen.findByRole("button", { name: "Plan the release" }));
    expect(await screen.findByText("All green.")).toBeTruthy();
    for (const author of ["admin:tester", "orchestrator:agent-1", "system"]) {
      expect(screen.getAllByText(new RegExp(`^${author} ·`)).length).toBe(1);
    }
    expect(document.querySelectorAll("article time").length).toBe(3);
    expect(screen.getByText("~$1.25 this thread")).toBeTruthy();
  });

  it("shows a zero cost as $0.00, not as missing", async () => {
    net.conversations = [{ ...conversation, cost_usd: 0 }];
    renderChat();
    fireEvent.click(await screen.findByRole("button", { name: "Plan the release" }));
    expect(await screen.findByText("~$0.00 this thread")).toBeTruthy();
  });

  it("omits the cost when the conversation has none", async () => {
    net.conversations = [{ ...conversation, cost_usd: null }];
    renderChat();
    fireEvent.click(await screen.findByRole("button", { name: "Plan the release" }));
    expect(await screen.findByText("All green.")).toBeTruthy();
    expect(screen.queryByText(/this thread/)).toBeNull();
  });
});

describe("Chat orchestrator status", () => {
  const text = () => screen.findByRole("status").then((el) => el.textContent ?? "");
  const expectStatus = (re: RegExp) => waitFor(async () => expect(await text()).toMatch(re));

  it("says Registered, never Live, and names what is not checked", async () => {
    renderChat();
    await expectStatus(/^Orchestrator: Registered · Heartbeat \d+ s ago\./);
    const t = await text();
    expect(t).toMatch(/Readiness is not checked\. Budget unknown\. A harness account is free\./);
    expect(t).not.toMatch(/Live|Ready/);
  });

  it("is not Registered from an agent list alone: a missing agent is Starting", async () => {
    net.agents = [];
    renderChat();
    await expectStatus(/Starting · Waiting for the orchestrator to register/);
  });

  it("ignores agents of other roles or instances", async () => {
    net.agents = [agent({ role: "worker" }), agent({ id: "a2", instance_id: "other" })];
    renderChat();
    await expectStatus(/Starting · Waiting/);
  });

  it("calls an agent that missed its heartbeats Lost, as the spec does", async () => {
    net.agents = [agent({ state: "lost", end_reason: "heartbeat timeout" })];
    renderChat();
    await expectStatus(/^Orchestrator: Lost · heartbeat timeout/);
  });

  it("words the fallback by state when end_reason is null", async () => {
    net.agents = [agent({ state: "gone", end_reason: null })];
    const first = renderChat();
    await expectStatus(/^Orchestrator: Ended · The orchestrator left\.$/);
    first.unmount();

    net.agents = [agent({ state: "lost", end_reason: null })];
    renderChat();
    await expectStatus(/^Orchestrator: Lost · The orchestrator stopped heartbeating\.$/);
  });

  it("keeps a gone agent distinct from a lost one", async () => {
    net.agents = [agent({ state: "gone", end_reason: "left" })];
    renderChat();
    await expectStatus(/^Orchestrator: Ended · left/);
  });

  it("flags a stale heartbeat on an active agent", async () => {
    net.agents = [agent({ last_heartbeat_at: new Date(Date.now() - 200_000).toISOString() })];
    renderChat();
    await expectStatus(/^Orchestrator: Stale · No heartbeat for 2\d\d s/);
    expect(await text()).not.toMatch(/Registered/);
  });

  it("flags a draining agent or instance", async () => {
    net.agents = [agent({ state: "draining" })];
    const first = renderChat();
    await expectStatus(/^Orchestrator: Draining/);
    first.unmount();

    net.agents = [agent()];
    net.instances = [instance({ draining: true })];
    renderChat();
    await expectStatus(/^Orchestrator: Draining/);
  });

  it("distinguishes an unreachable pool from a failing one", async () => {
    net.pools = [pool({ status: "unreachable", last_error: "docker host down" })];
    const first = renderChat();
    await expectStatus(/^Orchestrator: Unreachable · docker host down/);
    first.unmount();

    net.pools = [pool({ status: "failing", last_error: "image pull failed" })];
    renderChat();
    await expectStatus(/^Orchestrator: Offline · image pull failed/);
  });

  it("is Starting with no running instance or no pool", async () => {
    net.instances = [instance({ state: "reserved" })];
    const first = renderChat();
    await expectStatus(/Starting · No instance is running/);
    first.unmount();

    net.pools = [];
    renderChat();
    await expectStatus(/Starting · The orchestrator pool/);
  });

  it("reports unknown while a read is loading or failed, not Registered", async () => {
    const pending = new Promise<never>(() => {});
    net.pools = pending as unknown as unknown[];
    renderChat();
    expect(await text()).toBe("Orchestrator: Status unknown · Loading registration state.");
  });

  it("treats unavailable engine data as unknown capacity, not free", async () => {
    net.failEngines = true;
    renderChat();
    await expectStatus(/Registered · .*Budget unknown\. Account capacity unknown\./);
  });

  it("counts only harness accounts: executor-only or repo-only ones do not free it", async () => {
    // Executor layer has a leasable account; harness layer has none; a repo-only
    // provider has no engine entry at all.
    net.engines = catalog(
      [layer("executor", { leasable: 3 })],
      [layer("harness", { leasable: 0 })],
    );
    renderChat();
    await expectStatus(/No harness account is free\./);
    expect(await text()).not.toMatch(/A harness account is free/);
  });

  it("reports a harness account at max_leases as busy, not free", async () => {
    net.engines = catalog([layer("harness", { leasable: 0, busy: 1 })]);
    renderChat();
    await expectStatus(/Every harness account is busy\./);
  });

  it("does not treat a null leasable (capacity not computed) as free", async () => {
    net.engines = catalog([layer("harness", { leasable: null })]);
    renderChat();
    await expectStatus(/No harness account is free\./);
  });

  it("also shows in an open thread", async () => {
    net.conversations = [conversation];
    net.messages.c0 = [message("m1", "admin:tester", "hi")];
    renderChat();
    fireEvent.click(await screen.findByRole("button", { name: "Plan the release" }));
    expect(await screen.findByText("hi")).toBeTruthy();
    await expectStatus(/^Orchestrator: Registered/);
  });

  describe("after a failed refetch", () => {
    const registered = () => expectStatus(/^Orchestrator: Registered/);
    const unknown = () =>
      expectStatus(/^Orchestrator: Status unknown · Loading registration state\./);

    it.each(["/api/pools", "/api/instances", "/api/agents"])(
      "drops Registered when %s fails and recovers when it succeeds",
      async (path) => {
        renderChat();
        await registered();

        net.fail.add(path);
        await emit();
        await unknown();
        expect(await text()).not.toMatch(/Registered/);

        net.fail.delete(path);
        await emit();
        await registered();
      },
    );

    it("falls back to unknown capacity, keeping Registered, when engines fail", async () => {
      renderChat();
      await expectStatus(/A harness account is free\./);

      net.fail.add("/api/engines");
      await emit();
      await expectStatus(/Registered · .*Account capacity unknown\./);
      expect(await text()).not.toMatch(/harness account is free/);

      net.fail.delete("/api/engines");
      await emit();
      await expectStatus(/A harness account is free\./);
    });
  });
});
