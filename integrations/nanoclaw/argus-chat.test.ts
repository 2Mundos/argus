import { afterEach, describe, expect, it, vi } from "vitest";
import type { ChannelSetup } from "./adapter.js";
import {
  type ArgusConfig,
  authRejectedReply,
  createArgusAdapter,
  noAccountReply,
} from "./argus.js";
import { argusMcpUrl, OneCliClient } from "./argus-accounts.js";
import {
  chatThread,
  createChatLoop,
  parseChatThread,
  renderChatInbound,
  renderChatSeed,
} from "./argus-chat.js";

/** A long-poll that stays open until the loop aborts it. */
const hang = (signal?: AbortSignal) =>
  new Promise<never>((_, reject) => {
    const abort = () => reject(Object.assign(new Error("aborted"), { name: "AbortError" }));
    if (signal?.aborted) abort();
    else signal?.addEventListener("abort", abort);
  });

describe("chat thread ids", () => {
  it("round-trips conversation ids", () => {
    expect(chatThread("c1")).toBe("argus-chat:c1");
    expect(parseChatThread("argus-chat:c1")).toBe("c1");
    expect(parseChatThread("argus:task:1")).toBeNull();
    expect(parseChatThread(null)).toBeNull();
  });
});

describe("renderChatInbound", () => {
  it("frames the admin message as a trusted instruction", () => {
    const text = renderChatInbound({
      message: {
        id: "m1",
        conversation_id: "c1",
        author: "admin:alice",
        body: "Ship the fix",
        state: "delivered",
        turn_id: "t1",
        created_at: "2026-10-20T12:00:00Z",
      },
      conversation: {
        id: "c1",
        kind: "chat",
        title: "Login",
        session_ref: null,
      },
    });
    expect(text).toContain("Trusted operator instruction from admin:alice");
    expect(text).toContain("Ship the fix");
    expect(text).toContain("untrusted DATA");
  });

  it("frames a system inbox wake as a notice, not an admin instruction", () => {
    const text = renderChatInbound({
      message: {
        id: "m2",
        conversation_id: "c1",
        author: "system",
        body: "Inbox item waiting.\ndecision_id: d1\nkind: question\naudience: any",
        state: "delivered",
        turn_id: "t2",
        created_at: "2026-10-20T12:00:00Z",
      },
      conversation: {
        id: "c1",
        kind: "updates",
        title: "Updates",
        session_ref: null,
      },
    });
    expect(text).toContain("Inbox wake from Argus, not an admin");
    expect(text).not.toContain("Trusted operator instruction");
    expect(text).toContain("decision_id: d1");
    expect(text).toContain("untrusted DATA");
  });
});

describe("renderChatSeed", () => {
  it("lists recent messages and originated tasks", () => {
    const text = renderChatSeed({
      messages: [
        {
          id: "m1",
          conversation_id: "c1",
          author: "admin:a",
          body: "hi",
          state: "answered",
          turn_id: null,
          created_at: "2026-10-20T12:00:00Z",
        },
      ],
      tasks: [{ id: "task-1", title: "Fix login", state: "ready" }],
    });
    expect(text).toContain("Session restored from history");
    expect(text).toContain("task-1");
    expect(text).toContain("hi");
    expect(text).toContain("untrusted DATA");
  });
});

describe("createChatLoop", () => {
  it("gives the inbound a millisecond ISO timestamp, whatever precision Argus sent", async () => {
    let claimed = false;
    const stamps: string[] = [];
    let stopped = false;
    const loop = createChatLoop({
      call: async (method, path, _body, signal) => {
        if (method === "GET" && path.startsWith("/agents/me/chat?")) {
          if (claimed) await hang(signal);
          claimed = true;
          return {
            status: 200,
            data: {
              message: {
                id: "m1",
                conversation_id: "c1",
                author: "admin:a",
                body: "Do it",
                state: "delivered",
                turn_id: "turn-1",
                created_at: "2026-10-31T12:16:18.389852+00:00",
              },
              conversation: { id: "c1", kind: "chat", title: "Goal", session_ref: "argus-chat:c1" },
            },
          };
        }
        return { status: 200, data: {} };
      },
      onInbound: async (_p, _t, m) => void stamps.push(m.timestamp),
      platformId: "local",
      isStopped: () => stopped,
      claimWaitS: 1,
      sleep: async () => undefined,
      log: { info: () => undefined, warn: () => undefined },
    });
    const running = loop.loop();
    await vi.waitFor(() => expect(stamps).toEqual(["2026-10-31T12:16:18.389Z"]));
    stopped = true;
    loop.stop();
    await running;
  });

  it("claims, injects, and posts a reply with usage", async () => {
    const calls: { method: string; path: string; body?: unknown }[] = [];
    let claimOnce = true;
    const inbound: string[] = [];
    const sleep = vi.fn(async () => undefined);
    let stopped = false;

    const loop = createChatLoop({
      call: async (method, path, body, signal) => {
        calls.push({ method, path, body });
        if (method === "GET" && path.startsWith("/agents/me/chat?")) {
          if (!claimOnce) {
            await hang(signal); // hang until stop
          }
          claimOnce = false;
          return {
            status: 200,
            data: {
              message: {
                id: "m1",
                conversation_id: "c1",
                author: "admin:a",
                body: "Do it",
                state: "delivered",
                turn_id: "turn-1",
                created_at: "2026-10-20T12:00:00Z",
              },
              conversation: {
                id: "c1",
                kind: "chat",
                title: "Goal",
                session_ref: "argus-chat:c1",
              },
            },
          };
        }
        if (method === "POST" && path === "/agents/me/chat/c1") {
          return { status: 200, data: { id: "reply-1" } };
        }
        return { status: 200, data: {} };
      },
      onInbound: async (_p, _t, m) => {
        inbound.push(m.content.text);
      },
      platformId: "local",
      isStopped: () => stopped,
      claimWaitS: 1,
      sleep,
      log: { info: () => undefined, warn: () => undefined },
    });

    const running = loop.loop();
    await vi.waitFor(() => expect(inbound.length).toBe(1));
    expect(inbound[0]).toContain("Do it");

    const handled = await loop.deliverChat(chatThread("c1"), "Done.", {
      input_tokens: 10,
      output_tokens: 4,
      model: "claude-sonnet-5-5",
    });
    expect(handled).toBe(true);
    const reply = calls.find((c) => c.method === "POST" && c.path === "/agents/me/chat/c1");
    expect(reply?.body).toMatchObject({
      body: "Done.",
      turn_id: "turn-1",
      usage: { input_tokens: 10, output_tokens: 4, model: "claude-sonnet-5-5" },
    });

    stopped = true;
    loop.stop();
    await running;
  });

  it("folds the history seed into the first inbound and records the session on reply", async () => {
    const inbound: string[] = [];
    const calls: { method: string; path: string; body?: any }[] = [];
    let claimOnce = true;
    let stopped = false;
    const msg = (id: string, body: string) => ({
      id,
      conversation_id: "c2",
      author: "admin:a",
      body,
      state: "answered",
      turn_id: null,
      created_at: "2026-10-20T11:00:00Z",
    });
    const loop = createChatLoop({
      call: async (method, path, body, signal) => {
        calls.push({ method, path, body });
        if (method === "GET" && path.startsWith("/agents/me/chat?")) {
          if (!claimOnce) await hang(signal);
          claimOnce = false;
          return {
            status: 200,
            data: {
              message: { ...msg("m2", "Continue"), state: "delivered", turn_id: "t2" },
              conversation: { id: "c2", kind: "chat", title: "Resume", session_ref: null },
            },
          };
        }
        if (path.endsWith("/context")) {
          // The claimed message is already part of the history the API returns.
          return {
            status: 200,
            data: { messages: [msg("old", "earlier"), msg("m2", "Continue")], tasks: [] },
          };
        }
        return { status: 200, data: {} };
      },
      onInbound: async (_p, _t, m) => {
        inbound.push(m.content.text);
      },
      platformId: "local",
      isStopped: () => stopped,
      claimWaitS: 1,
      sleep: async () => undefined,
    });
    const running = loop.loop();
    await vi.waitFor(() => expect(inbound.length).toBe(1));
    expect(inbound[0]).toContain("Session restored from history");
    expect(inbound[0]).toContain("earlier");
    expect(inbound[0].match(/Continue/g)).toHaveLength(1);
    expect(calls.some((c) => c.method === "PUT")).toBe(false); // not before it answered

    await loop.deliverChat(chatThread("c2"), "ok");
    const put = calls.find((c) => c.method === "PUT");
    expect(put?.path).toBe("/agents/me/chat/c2/session");
    expect(put?.body).toMatchObject({ session_ref: chatThread("c2"), restored: true });
    stopped = true;
    loop.stop();
    await running;
  });

  it("does not record a session or seed again when the seed fetch fails", async () => {
    const inbound: string[] = [];
    const puts: unknown[] = [];
    const claims: any[] = [];
    const claim = (id: string) => ({
      message: {
        id,
        conversation_id: "c3",
        author: "admin:a",
        body: id,
        state: "delivered",
        turn_id: `t-${id}`,
        created_at: "2026-10-20T12:00:00Z",
      },
      conversation: { id: "c3", kind: "chat", title: "T", session_ref: null },
    });
    claims.push(claim("a"), claim("b"));
    let contextCalls = 0;
    let stopped = false;
    let openSecond!: () => void;
    const secondMayCome = new Promise<void>((r) => {
      openSecond = r;
    });
    const loop = createChatLoop({
      call: async (method, path, body, signal) => {
        if (method === "GET" && path.startsWith("/agents/me/chat?")) {
          if (claims.length === 1) await secondMayCome; // one turn per conversation
          const next = claims.shift();
          if (!next) await hang(signal);
          return { status: 200, data: next };
        }
        if (path.endsWith("/context")) {
          contextCalls += 1;
          throw new Error("boom");
        }
        if (method === "PUT") puts.push(body);
        return { status: 200, data: {} };
      },
      onInbound: async (_p, _t, m) => {
        inbound.push(m.content.text);
      },
      platformId: "local",
      isStopped: () => stopped,
      claimWaitS: 1,
      sleep: async () => undefined,
    });
    const running = loop.loop();
    await vi.waitFor(() => expect(inbound.length).toBe(1));
    await loop.deliverChat(chatThread("c3"), "r1");
    openSecond();
    await vi.waitFor(() => expect(inbound.length).toBe(2));
    expect(contextCalls).toBe(2); // retried, not remembered as seeded
    expect(puts[0]).toMatchObject({ restored: false });
    stopped = true;
    loop.stop();
    await running;
  });

  function singleClaimLoop(opts: {
    post: () => Promise<{ status: number; data: any }>;
    turnTimeoutMs?: number;
    claims?: number;
  }) {
    let n = opts.claims ?? 1;
    let stopped = false;
    const inbound: string[] = [];
    const loop = createChatLoop({
      call: async (method, path, _body, signal) => {
        if (method === "GET" && path.startsWith("/agents/me/chat?")) {
          if (n === 0) await hang(signal);
          n -= 1;
          return {
            status: 200,
            data: {
              message: {
                id: `m${n}`,
                conversation_id: "c1",
                author: "admin:a",
                body: `body ${n}`,
                state: "delivered",
                turn_id: `t${n}`,
                created_at: "2026-10-20T12:00:00Z",
              },
              conversation: { id: "c1", kind: "chat", title: "G", session_ref: "argus-chat:c1" },
            },
          };
        }
        if (method === "POST") return opts.post();
        return { status: 200, data: {} };
      },
      onInbound: async (_p, _t, m) => {
        inbound.push(m.content.text);
      },
      platformId: "local",
      isStopped: () => stopped,
      claimWaitS: 1,
      turnTimeoutMs: opts.turnTimeoutMs,
      sleep: async () => undefined,
      log: { info: () => undefined, warn: () => undefined },
    });
    const running = loop.loop();
    return {
      loop,
      inbound,
      stop: async () => {
        stopped = true;
        loop.stop();
        await running;
      },
    };
  }

  it("keeps the turn open for a redelivery after a transient POST failure", async () => {
    let attempts = 0;
    const h = singleClaimLoop({
      post: async () => {
        attempts += 1;
        if (attempts === 1) throw Object.assign(new Error("down"), { status: 503 });
        return { status: 200, data: {} };
      },
    });
    await vi.waitFor(() => expect(h.inbound.length).toBe(1));
    await expect(h.loop.deliverChat(chatThread("c1"), "x")).rejects.toThrow("down");
    expect(h.loop.active.has("c1")).toBe(true);
    await h.loop.deliverChat(chatThread("c1"), "x");
    await vi.waitFor(() => expect(h.loop.active.size).toBe(0));
    await h.stop();
  });

  it("ends the turn when the API refuses the reply", async () => {
    const h = singleClaimLoop({
      post: async () => {
        throw Object.assign(new Error("conflict"), { status: 409 });
      },
    });
    await vi.waitFor(() => expect(h.inbound.length).toBe(1));
    await expect(h.loop.deliverChat(chatThread("c1"), "x")).rejects.toThrow("conflict");
    await vi.waitFor(() => expect(h.loop.active.size).toBe(0));
    await h.stop();
  });

  it("frees the conversation when no reply arrives before the turn timeout", async () => {
    const h = singleClaimLoop({
      post: async () => ({ status: 200, data: {} }),
      turnTimeoutMs: 20,
    });
    await vi.waitFor(() => expect(h.inbound.length).toBe(1));
    await vi.waitFor(() => expect(h.loop.active.size).toBe(0));
    await h.stop();
  });

  it("injects a claim that arrives while a stale turn is held, instead of dropping it", async () => {
    const h = singleClaimLoop({
      post: async () => ({ status: 200, data: {} }),
      claims: 2,
    });
    await vi.waitFor(() => expect(h.inbound.length).toBe(2));
    expect(h.inbound[1]).toContain("body 0");
    expect(h.loop.active.get("c1")?.messageId).toBe("m0");
    await h.loop.deliverChat(chatThread("c1"), "done");
    await vi.waitFor(() => expect(h.loop.active.size).toBe(0));
    await h.stop();
  });
});

type Call = { method: string; path: string; body: any };

function fakeChatArgus(
  opts: { leaseFails?: number; accounts?: string[]; exhausted?: Record<string, unknown> } = {},
) {
  const accounts = [...(opts.accounts ?? ["a1"])];
  const calls: Call[] = [];
  const queue: any[] = [
    {
      message: {
        id: "m1",
        conversation_id: "c1",
        author: "admin:a",
        body: "Plan the rollout",
        state: "delivered",
        turn_id: "turn-1",
        created_at: "2026-10-20T12:00:00Z",
      },
      conversation: {
        id: "c1",
        kind: "chat",
        title: "Rollout",
        session_ref: chatThread("c1"),
      },
    },
  ];
  const fetchImpl = (async (url: string, init: RequestInit) => {
    const path = new URL(url).pathname.replace(/^\/api/, "") + (new URL(url).search || "");
    const body = init.body ? JSON.parse(init.body as string) : undefined;
    calls.push({ method: init.method ?? "GET", path, body });
    if (path === "/agents/register") {
      return new Response(
        JSON.stringify({
          token: "argus_agent_x",
          agent: { id: "orch-1", role: "orchestrator" },
          heartbeat_interval_s: 1,
        }),
        { status: 201 },
      );
    }
    if (path === "/agents/me/heartbeat") {
      return new Response(JSON.stringify({ state: "active", renewed: [], rejected: [] }), {
        status: 200,
      });
    }
    if (path === "/agents/me/leave") {
      return new Response(JSON.stringify({ state: "gone" }), { status: 200 });
    }
    if (path.startsWith("/agents/me/chat?")) {
      const next = queue.shift();
      if (next) return new Response(JSON.stringify(next), { status: 200 });
      return new Promise<Response>((_, reject) => {
        init.signal?.addEventListener("abort", () =>
          reject(new DOMException("aborted", "AbortError")),
        );
      });
    }
    if (path === "/accounts/lease") {
      if (opts.leaseFails && opts.leaseFails-- > 0) {
        return new Response(JSON.stringify({ detail: { code: "no_account" } }), { status: 409 });
      }
      const id = accounts.length > 1 ? accounts.shift()! : accounts[0]!;
      return new Response(
        JSON.stringify({
          lease_id: `l-${id}`,
          account_id: id,
          name: `claude ${id}`,
          provider: "anthropic",
          kind: "api_key",
          role: "engine",
          credential: { api_key: "sk-ant-api03-main" },
        }),
        { status: 200 },
      );
    }
    if (path.endsWith("/exhausted")) {
      return new Response(
        JSON.stringify(
          opts.exhausted ?? {
            next_account_id: accounts[0],
            next_account_name: `claude ${accounts[0]}`,
            next_provider: "anthropic",
            task_blocked: false,
          },
        ),
        { status: 200 },
      );
    }
    if (path === "/agents/me/chat/c1") {
      return new Response(JSON.stringify({ id: "r1", body: body?.body }), { status: 200 });
    }
    return new Response(JSON.stringify({}), { status: 200 });
  }) as unknown as typeof fetch;
  return { calls, fetchImpl };
}

const live: Array<{ teardown: () => Promise<void> }> = [];
afterEach(async () => {
  for (const a of live.splice(0)) await a.teardown();
});

describe("createArgusAdapter orchestrator mode", () => {
  it("registers as orchestrator, injects chat, and delivers replies", async () => {
    const fake = fakeChatArgus();
    const inbound: any[] = [];
    let thread: string | null = null;
    const setup = {
      onInbound: vi.fn(async (_p: string, t: string | null, m: any) => {
        thread = t;
        inbound.push(m);
      }),
      onInboundEvent: vi.fn(),
      onMetadata: vi.fn(),
      onAction: vi.fn(),
    } as unknown as ChannelSetup;
    const adapter = createArgusAdapter({
      url: "http://argus.test",
      bootstrapToken: "argus_boot_x",
      capabilities: [],
      name: "orch",
      fetch: fake.fetchImpl,
      claimWaitS: 1,
    } as ArgusConfig);
    live.push(adapter);
    await adapter.setup(setup);
    await vi.waitFor(() => expect(inbound.length).toBe(1));
    expect(thread).toBe(chatThread("c1"));
    expect(inbound[0].content.text).toContain("Plan the rollout");

    await adapter.deliver!("local", thread, {
      content: { text: "Here is the plan.", usage: { input_tokens: 1, output_tokens: 2 } },
    } as any);
    const posted = fake.calls.find((c) => c.method === "POST" && c.path === "/agents/me/chat/c1");
    expect(posted?.body).toMatchObject({
      body: "Here is the plan.",
      turn_id: "turn-1",
      usage: { input_tokens: 1, output_tokens: 2 },
    });
  });
});

/** A OneCLI vault that records the assignment and mode of the one agent. */
function fakeVault() {
  const state = { secretIds: [] as string[], mode: "all", secrets: [] as any[] };
  const fetchImpl = (async (url: string, init: RequestInit) => {
    const path = new URL(url).pathname.replace(/^\/api/, "");
    const method = init.method ?? "GET";
    const body = init.body ? JSON.parse(init.body as string) : undefined;
    const ok = (data: unknown) => new Response(JSON.stringify(data), { status: 200 });
    if (path === "/agents" && method === "GET") return ok([{ id: "oa", identifier: "g" }]);
    if (path === "/secrets" && method === "GET") return ok(state.secrets);
    if (path === "/secrets" && method === "POST") {
      const secret = { id: `s${state.secrets.length}`, ...body };
      state.secrets.push(secret);
      return ok(secret);
    }
    if (path === "/agents/oa/secrets" && method === "GET") return ok(state.secretIds);
    if (path === "/agents/oa/secrets") state.secretIds = body.secretIds;
    if (path.startsWith("/secrets/") && method === "PATCH") {
      const secret = state.secrets.find((x) => `/secrets/${x.id}` === path);
      Object.assign(secret, body);
      return ok(secret);
    }
    if (path === "/agents/oa/secret-mode") state.mode = body.mode;
    return ok({});
  }) as unknown as typeof fetch;
  return { state, onecli: new OneCliClient("http://127.0.0.1:10254", fetchImpl) };
}

describe("createArgusAdapter orchestrator accounts", () => {
  it("leases and applies its engine before it claims chat (spec 3.1)", async () => {
    const fake = fakeChatArgus({ leaseFails: 1 });
    const vault = fakeVault();
    const inbound: any[] = [];
    const selectEngine = vi.fn(async () => false);
    const adapter = createArgusAdapter({
      url: "http://argus.test",
      bootstrapToken: "argus_boot_x",
      capabilities: [],
      name: "orch",
      fetch: fake.fetchImpl,
      claimWaitS: 1,
      accounts: {
        models: true,
        onecli: vault.onecli,
        resolveTarget: async () => ({ identifier: "g", name: "orch" }),
        selectEngine,
      },
    } as ArgusConfig);
    live.push(adapter);
    vi.useFakeTimers({ toFake: ["setTimeout"] });
    try {
      await adapter.setup({
        onInbound: async (_p: string, _t: string | null, m: any) => void inbound.push(m),
        onInboundEvent: vi.fn(),
        onMetadata: vi.fn(),
        onAction: vi.fn(),
      } as unknown as ChannelSetup);
      await vi.waitFor(() =>
        expect(fake.calls.filter((c) => c.path === "/accounts/lease").length).toBe(1),
      );
      // No account yet: chat is not claimed.
      expect(fake.calls.some((c) => c.path.startsWith("/agents/me/chat?"))).toBe(false);
      await vi.advanceTimersByTimeAsync(6_000);
    } finally {
      vi.useRealTimers();
    }
    await vi.waitFor(() => expect(inbound.length).toBe(1));
    const leased = fake.calls.filter((c) => c.path === "/accounts/lease");
    expect(leased.at(-1)?.body).toEqual({ runtime_providers: expect.any(Array) });
    expect(vault.state.mode).toBe("selective");
    expect(vault.state.secretIds).toHaveLength(1);
    expect(selectEngine).toHaveBeenCalledWith("claude");
  });
});

describe("createArgusAdapter orchestrator failures", () => {
  async function orchestrator(
    opts: Parameters<typeof fakeChatArgus>[0],
    lastApiError?: ArgusConfig["lastApiError"],
  ) {
    const fake = fakeChatArgus(opts);
    const vault = fakeVault();
    const inbound: any[] = [];
    const selectModel = vi.fn(async () => false);
    const adapter = createArgusAdapter({
      url: "http://argus.test",
      bootstrapToken: "argus_boot_x",
      capabilities: [],
      name: "orch",
      fetch: fake.fetchImpl,
      claimWaitS: 1,
      accounts: {
        models: true,
        onecli: vault.onecli,
        resolveTarget: async () => ({ identifier: "g", name: "orch" }),
        selectEngine: async () => false,
        selectModel,
      },
      lastApiError,
    } as ArgusConfig);
    live.push(adapter);
    await adapter.setup({
      onInbound: async (_p: string, _t: string | null, m: any) => void inbound.push(m),
      onInboundEvent: vi.fn(),
      onMetadata: vi.fn(),
      onAction: vi.fn(),
    } as unknown as ChannelSetup);
    // The chat loop looks again a second after the account is applied.
    await vi.waitFor(() => expect(inbound.length).toBe(1), { timeout: 5_000 });
    const fail = (providerFailure: unknown) =>
      adapter.deliver!("local", chatThread("c1"), {
        content: {
          text: "The agent run failed. Check the logs for details.",
          failureNotice: true,
          providerFailure,
        },
      } as any);
    const replies = () =>
      fake.calls.filter((c) => c.method === "POST" && c.path === "/agents/me/chat/c1");
    return { fake, inbound, fail, replies, selectModel };
  }

  const LIMIT = { provider: "claude", kind: "rate_limit", resets_at: "2099-01-01T00:00:00Z" };

  it("exhausts the account, switches and sends the turn again", async () => {
    const h = await orchestrator({ accounts: ["a1", "a2"] });
    await h.fail(LIMIT);
    const exhausted = h.fake.calls.find((c) => c.path === "/accounts/a1/exhausted");
    expect(exhausted?.body).toMatchObject({ role: "engine", reason_code: "quota_exhausted" });
    expect(exhausted?.body.task_id).toBeUndefined();
    expect(h.fake.calls.filter((c) => c.path === "/accounts/lease")).toHaveLength(2);
    expect(h.inbound).toHaveLength(2);
    expect(h.inbound[1].id).toBe(`${h.inbound[0].id}-r1`);
    expect(h.inbound[1].content.text).toContain("account was switched");
    expect(h.inbound[1].content.text).toContain("Plan the rollout");
    expect(h.replies()).toHaveLength(0); // the failure is not the answer
    expect(h.selectModel).toHaveBeenLastCalledWith("claude", "anthropic", null);

    // A second failure of the same turn is not sent a third time.
    await h.fail(LIMIT);
    expect(h.inbound).toHaveLength(2);
  });

  it("replies with the earliest reset when no account is left", async () => {
    const h = await orchestrator({
      exhausted: { next_account_id: null, earliest_reset: "2099-01-01T00:00:00Z" },
    });
    await h.fail(LIMIT);
    expect(h.replies().at(-1)?.body.body).toBe(noAccountReply("2099-01-01T00:00:00Z"));
    expect(h.inbound).toHaveLength(1);
  });

  it("names the provider's API error behind a generic failure", async () => {
    const since: number[] = [];
    const h = await orchestrator({}, async (s) => {
      since.push(s);
      return { status: 401, type: "authentication_error" };
    });
    await h.fail(undefined);
    expect(h.replies().at(-1)?.body.body).toBe(
      "The agent run failed: Claude API error 401 (authentication_error).",
    );
    expect(since[0]).toBeGreaterThan(0); // bounded by the turn's start
  });

  it("records a rejected credential and names the account", async () => {
    const h = await orchestrator({});
    await h.fail({ provider: "claude", kind: "auth_expired" });
    const rejected = h.fake.calls.find((c) => c.path === "/accounts/a1/auth-rejected");
    expect(rejected?.body).toEqual({ role: "engine", source: "structured" });
    expect(h.replies().at(-1)?.body.body).toBe(
      authRejectedReply({ name: "claude a1", provider: "anthropic" }),
    );
  });
});

describe("orchestrator Argus MCP wiring (spec 3.1)", () => {
  it("accepts only an endpoint NanoClaw accepts", () => {
    expect(argusMcpUrl("https://argus.example/")?.href).toBe("https://argus.example/mcp");
    expect(argusMcpUrl("https://argus.example/base?x=1")?.href).toBe(
      "https://argus.example/base/mcp",
    );
    expect(argusMcpUrl("http://host.docker.internal:8000")?.href).toBe(
      "http://host.docker.internal:8000/mcp",
    );
    expect(argusMcpUrl("http://argus.lan:8000")).toBeNull();
    expect(argusMcpUrl("not a url")).toBeNull();
  });

  it("puts the token in OneCLI for /mcp only, wires the server, and keeps it through the lease", async () => {
    const fake = fakeChatArgus();
    const vault = fakeVault();
    const inbound: any[] = [];
    const setArgusMcp = vi.fn(async () => undefined);
    const adapter = createArgusAdapter({
      url: "https://argus.test",
      bootstrapToken: "argus_boot_x",
      capabilities: [],
      name: "orch",
      fetch: fake.fetchImpl,
      claimWaitS: 1,
      accounts: {
        models: true,
        onecli: vault.onecli,
        resolveTarget: async () => ({ identifier: "g", name: "orch" }),
        selectEngine: async () => false,
        setArgusMcp,
      },
    } as ArgusConfig);
    live.push(adapter);
    await adapter.setup({
      onInbound: async (_p: string, _t: string | null, m: any) => void inbound.push(m),
      onInboundEvent: vi.fn(),
      onMetadata: vi.fn(),
      onAction: vi.fn(),
    } as unknown as ChannelSetup);
    await vi.waitFor(() => expect(inbound.length).toBe(1), { timeout: 5_000 });
    expect(setArgusMcp).toHaveBeenCalledWith("https://argus.test/mcp");
    const mcp = vault.state.secrets.find((x) => x.name === "argus-g-mcp");
    expect(mcp).toMatchObject({
      type: "generic",
      value: "argus_agent_x",
      hostPattern: "argus.test",
      pathPattern: "/mcp",
      injectionConfig: { headerName: "Authorization", valueFormat: "Bearer {value}" },
    });
    // The account lease that followed kept it assigned, beside the account.
    expect(vault.state.secretIds).toContain(mcp.id);
    expect(vault.state.secretIds).toHaveLength(2);
    expect(vault.state.mode).toBe("selective");
  });

  it("posts a reply that arrives after its turn ended", async () => {
    const fake = fakeChatArgus();
    const inbound: any[] = [];
    const adapter = createArgusAdapter({
      url: "http://argus.test",
      bootstrapToken: "argus_boot_x",
      capabilities: [],
      name: "orch",
      fetch: fake.fetchImpl,
      claimWaitS: 1,
    } as ArgusConfig);
    live.push(adapter);
    await adapter.setup({
      onInbound: async (_p: string, _t: string | null, m: any) => void inbound.push(m),
      onInboundEvent: vi.fn(),
      onMetadata: vi.fn(),
      onAction: vi.fn(),
    } as unknown as ChannelSetup);
    await vi.waitFor(() => expect(inbound.length).toBe(1));
    await adapter.deliver!("local", chatThread("c1"), { content: { text: "Done." } } as any);
    await adapter.deliver!("local", chatThread("c1"), {
      content: { text: "Correction: it failed." },
    } as any);
    const posts = fake.calls.filter((c) => c.method === "POST" && c.path === "/agents/me/chat/c1");
    expect(posts.map((c) => [c.body.body, c.body.turn_id])).toEqual([
      ["Done.", "turn-1"],
      ["Correction: it failed.", null],
    ]);
  });
});
