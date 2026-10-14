/**
 * Argus channel — makes a NanoClaw agent group a worker of an Argus fleet.
 *
 * Installed into a NanoClaw checkout by `integrations/nanoclaw/install.sh`
 * (copied to src/channels/argus.ts). The channel runs in the NanoClaw host
 * process and speaks the Argus worker API; the agent container never sees an
 * Argus token. It:
 *
 *   1. registers with Argus using a bootstrap token (or uses ARGUS_AGENT_TOKEN),
 *   2. heartbeats, renewing the lease on the task it holds,
 *   3. long-polls for a task and injects it as an inbound message,
 *   4. turns the agent's replies back into Argus calls:
 *        plain reply        -> progress note
 *        "COMPLETE: ..."    -> complete the task (the rest is the summary)
 *        "FAIL: ..."        -> fail the task
 *        "ASK: ..."         -> ask a question; the task waits for an answer
 *
 * One task at a time. Environment (the Argus `plain` runtime contract):
 *   ARGUS_URL                 required; the channel is off without it
 *   ARGUS_BOOTSTRAP_TOKENS    comma-separated; the first is used
 *   ARGUS_AGENT_TOKEN         use an already-registered agent token instead
 *   ARGUS_CAPABILITIES        comma-separated capabilities to claim with
 *   ARGUS_AGENT_NAME          display name (default: the NanoClaw host name)
 */
import { log } from "../log.js";
import type { ChannelAdapter, ChannelDefaults, ChannelSetup, OutboundMessage } from "./adapter.js";
import { registerChannelAdapter } from "./channel-registry.js";

export const PLATFORM_ID = "local";
export const CHANNEL_TYPE = "argus";

const ARGUS_DEFAULTS: ChannelDefaults = {
  dm: { engageMode: "pattern", engagePattern: ".", threads: false, unknownSenderPolicy: "public" },
  group: {
    engageMode: "pattern",
    engagePattern: ".",
    threads: false,
    unknownSenderPolicy: "public",
  },
  mentions: "never",
};

const PROGRESS_MIN_INTERVAL_MS = 10_000; // Argus allows one progress note per 10 s
const CLAIM_WAIT_S = 20;
const MAX_PAGE_CHARS = 4000;
const MAX_PAGES_INLINED = 3;

export interface ArgusConfig {
  url: string;
  bootstrapToken?: string;
  agentToken?: string;
  capabilities: string[];
  name: string;
  /** Test seams. */
  fetch?: typeof fetch;
  now?: () => number;
  progressIntervalMs?: number;
  claimWaitS?: number;
}

interface KnowledgeRef {
  page_id: string;
  title: string;
  summary: string;
  why: "pinned" | "match";
  stale: boolean;
}

interface ArgusTask {
  id: string;
  title: string;
  spec: string;
  project: string;
  attempt: number;
  acceptance: string[];
  knowledge_refs: KnowledgeRef[];
}

export type Directive = { kind: "complete" | "fail" | "ask" | "progress"; text: string };

/** Interpret an agent reply. Directives are matched at the start, case-insensitively. */
export function parseReply(raw: string): Directive {
  const text = raw.trim();
  const m = /^(complete|fail|ask)\s*:\s*/i.exec(text);
  if (!m) return { kind: "progress", text };
  return { kind: m[1]!.toLowerCase() as Directive["kind"], text: text.slice(m[0].length).trim() };
}

/** The message a claimed task becomes. `pages` are inlined knowledge bodies by page id. */
export function renderTask(task: ArgusTask, pages: Map<string, string>): string {
  const lines = [
    `[Argus task] ${task.title}`,
    `Project: ${task.project} · Task: ${task.id} · Attempt: ${task.attempt}`,
    "",
    task.spec.trim() || "(no further description)",
  ];
  if (task.acceptance.length > 0) {
    lines.push("", "Acceptance criteria:", ...task.acceptance.map((a) => `- ${a}`));
  }
  if (task.knowledge_refs.length > 0) {
    lines.push("", "Relevant knowledge:");
    for (const ref of task.knowledge_refs) {
      lines.push(
        `- ${ref.title}${ref.stale ? " (may be out of date)" : ""}: ${ref.summary || "(no summary)"}`,
      );
      const body = pages.get(ref.page_id);
      if (body) {
        lines.push(...body.split("\n").map((l) => `    ${l}`));
      }
    }
  }
  lines.push(
    "",
    "How to respond: send short progress updates as normal messages. When you are done, reply with a",
    'message starting with "COMPLETE:" followed by a summary of what you did. If you cannot do it,',
    'start with "FAIL:" and say why. If you need someone to decide something, start with "ASK:" and',
    "ask the question; you will be given the answer.",
  );
  return lines.join("\n");
}

class ArgusError extends Error {
  constructor(
    readonly status: number,
    readonly code: string,
    message: string,
  ) {
    super(message);
  }
}

class ArgusClient {
  token = "";
  constructor(private readonly cfg: ArgusConfig) {}

  async call(
    method: string,
    path: string,
    body?: unknown,
    signal?: AbortSignal,
  ): Promise<{ status: number; data: any }> {
    const doFetch = this.cfg.fetch ?? fetch;
    const res = await doFetch(this.cfg.url.replace(/\/$/, "") + "/api" + path, {
      method,
      headers: {
        "Content-Type": "application/json",
        ...(this.token ? { Authorization: `Bearer ${this.token}` } : {}),
      },
      body: body === undefined ? undefined : JSON.stringify(body),
      signal,
    });
    const text = await res.text();
    const data = text ? JSON.parse(text) : null;
    if (res.status >= 400) {
      const d = data?.detail;
      throw new ArgusError(
        res.status,
        d?.code ?? "error",
        d?.message ?? String(d ?? res.statusText),
      );
    }
    return { status: res.status, data };
  }
}

export function createArgusAdapter(cfg: ArgusConfig): ChannelAdapter {
  const client = new ArgusClient(cfg);
  const nowMs = cfg.now ?? Date.now;
  const progressInterval = cfg.progressIntervalMs ?? PROGRESS_MIN_INTERVAL_MS;

  let setupCfg: ChannelSetup | null = null;
  let stopped = true;
  let draining = false;
  let registered = false;
  let current: { id: string; attempt: number } | null = null;
  let abortClaim: AbortController | null = null;
  let heartbeatTimer: ReturnType<typeof setInterval> | null = null;
  let loop: Promise<void> | null = null;
  let lastProgressAt = 0;
  let pendingProgress: string | null = null;
  let progressTimer: ReturnType<typeof setTimeout> | null = null;
  let taskDone: (() => void) | null = null;

  const sleep = (ms: number) => new Promise<void>((r) => setTimeout(r, ms));

  async function register(): Promise<number> {
    if (cfg.agentToken) {
      client.token = cfg.agentToken;
      return 30;
    }
    if (!cfg.bootstrapToken)
      throw new Error("ARGUS_BOOTSTRAP_TOKENS or ARGUS_AGENT_TOKEN is required");
    const { data } = await client.call("POST", "/agents/register", {
      bootstrap_token: cfg.bootstrapToken,
      name: cfg.name,
      capabilities: cfg.capabilities,
    });
    client.token = data.token;
    log.info("Argus channel registered", { agentId: data.agent.id });
    return data.heartbeat_interval_s ?? 30;
  }

  async function heartbeat(): Promise<void> {
    try {
      const { data } = await client.call("POST", "/agents/me/heartbeat", {
        leases: current ? [{ task_id: current.id, attempt: current.attempt }] : [],
      });
      if (data.state === "draining") draining = true;
      if (current && data.rejected?.some((r: any) => r.task_id === current!.id)) {
        log.warn("Argus lease on the task was rejected; abandoning it", { task: current.id });
        finishTask();
      }
    } catch (err) {
      if (err instanceof ArgusError && err.status === 401) {
        log.error("Argus rejected this agent token; the channel stops", { err });
        stopped = true;
        abortClaim?.abort();
        finishTask();
      } else {
        log.warn("Argus heartbeat failed (will retry)", { err });
      }
    }
  }

  function finishTask(): void {
    current = null;
    pendingProgress = null;
    if (progressTimer) clearTimeout(progressTimer);
    progressTimer = null;
    const done = taskDone;
    taskDone = null;
    done?.();
  }

  async function inlinePages(task: ArgusTask): Promise<Map<string, string>> {
    const pages = new Map<string, string>();
    const wanted = [...task.knowledge_refs].sort(
      (a, b) => (a.why === "pinned" ? -1 : 1) - (b.why === "pinned" ? -1 : 1),
    );
    for (const ref of wanted.slice(0, MAX_PAGES_INLINED)) {
      try {
        const { data } = await client.call("GET", `/knowledge/pages/${ref.page_id}`);
        const body: string = data.body ?? "";
        pages.set(
          ref.page_id,
          body.length > MAX_PAGE_CHARS ? body.slice(0, MAX_PAGE_CHARS) + "\n…(truncated)" : body,
        );
      } catch (err) {
        log.warn("Could not read a knowledge page for a task", { page: ref.page_id, err });
      }
    }
    return pages;
  }

  async function runTask(task: ArgusTask): Promise<void> {
    current = { id: task.id, attempt: task.attempt };
    lastProgressAt = 0;
    const finished = new Promise<void>((resolve) => {
      taskDone = resolve;
    });
    const pages = await inlinePages(task);
    log.info("Argus task claimed", { task: task.id, attempt: task.attempt });
    await setupCfg!.onInbound(PLATFORM_ID, null, {
      id: `argus-${task.id}-${task.attempt}`,
      kind: "chat",
      timestamp: new Date().toISOString(),
      content: { text: renderTask(task, pages), sender: "argus", senderId: `argus:${PLATFORM_ID}` },
    });
    await finished;
  }

  async function claimLoop(): Promise<void> {
    let backoff = 1000;
    while (!stopped) {
      if (draining) {
        await sleep(1000);
        continue;
      }
      abortClaim = new AbortController();
      try {
        const { status, data } = await client.call(
          "POST",
          "/tasks/claim",
          { capabilities: cfg.capabilities, wait_s: cfg.claimWaitS ?? CLAIM_WAIT_S },
          abortClaim.signal,
        );
        backoff = 1000;
        if (status === 200 && data) await runTask(data as ArgusTask);
      } catch (err) {
        if (stopped) return;
        log.warn("Argus claim failed (will retry)", { err });
        await sleep(backoff);
        backoff = Math.min(backoff * 2, 30_000);
      }
    }
  }

  async function sendProgress(text: string): Promise<void> {
    const task = current;
    if (!task) return;
    lastProgressAt = nowMs(); // count the interval from the send, not the response
    try {
      await client.call("POST", `/tasks/${task.id}/progress`, {
        attempt: task.attempt,
        message: text.slice(0, 2000),
      });
    } catch (err) {
      if (err instanceof ArgusError && err.status === 429)
        pendingProgress = text; // retry on the timer
      else log.warn("Argus progress note failed", { err });
    }
  }

  function queueProgress(text: string): void {
    const wait = lastProgressAt + progressInterval - nowMs();
    if (wait <= 0) {
      void sendProgress(text);
      return;
    }
    pendingProgress = text; // coalesce: only the latest note is worth sending
    if (!progressTimer) {
      progressTimer = setTimeout(() => {
        progressTimer = null;
        const t = pendingProgress;
        pendingProgress = null;
        if (t) void sendProgress(t);
      }, wait);
    }
  }

  const adapter: ChannelAdapter = {
    name: CHANNEL_TYPE,
    channelType: CHANNEL_TYPE,
    supportsThreads: false,
    defaults: ARGUS_DEFAULTS,

    async setup(config: ChannelSetup): Promise<void> {
      setupCfg = config;
      const interval = await register();
      registered = true;
      stopped = false;
      heartbeatTimer = setInterval(() => void heartbeat(), Math.max(interval, 1) * 1000);
      heartbeatTimer.unref?.();
      loop = claimLoop();
      void loop.catch((err) => log.error("Argus claim loop crashed", { err }));
    },

    async teardown(): Promise<void> {
      stopped = true;
      abortClaim?.abort();
      if (heartbeatTimer) clearInterval(heartbeatTimer);
      heartbeatTimer = null;
      finishTask();
      if (registered) {
        try {
          await client.call("POST", "/agents/me/leave");
        } catch {
          // best effort: the reaper ends us if this fails
        }
        registered = false;
      }
    },

    isConnected(): boolean {
      return registered && !stopped;
    },

    async deliver(
      platformId: string,
      _threadId: string | null,
      message: OutboundMessage,
    ): Promise<string | undefined> {
      if (platformId !== PLATFORM_ID) return undefined;
      const task = current;
      const content = message.content as Record<string, unknown> | string | undefined;
      const text =
        typeof content === "string"
          ? content
          : typeof content?.text === "string"
            ? content.text
            : null;
      if (!text?.trim()) return undefined;
      if (!task) {
        log.warn("Argus channel got a reply with no task in flight; dropped", {
          text: text.slice(0, 80),
        });
        return undefined;
      }
      const d = parseReply(text);
      try {
        if (d.kind === "complete") {
          await client.call("POST", `/tasks/${task.id}/complete`, {
            attempt: task.attempt,
            result: { summary: d.text || "(no summary)" },
          });
          finishTask();
        } else if (d.kind === "fail") {
          await client.call("POST", `/tasks/${task.id}/fail`, {
            attempt: task.attempt,
            error: d.text || "failed",
          });
          finishTask();
        } else if (d.kind === "ask") {
          await client.call("POST", `/tasks/${task.id}/ask`, {
            attempt: task.attempt,
            question: d.text || "?",
          });
          finishTask();
        } else {
          queueProgress(d.text);
        }
      } catch (err) {
        if (err instanceof ArgusError && err.status === 409) {
          log.warn("Argus says this task is no longer ours; dropping the reply", {
            task: task.id,
            code: err.code,
          });
          finishTask();
        } else {
          throw err; // let the delivery retry path handle transient failures
        }
      }
      return undefined;
    },
  };
  return adapter;
}

export function fromEnv(): ChannelAdapter | null {
  const url = process.env.ARGUS_URL?.trim();
  if (!url) return null;
  const bootstrap = process.env.ARGUS_BOOTSTRAP_TOKENS?.split(",")[0]?.trim();
  const agentToken = process.env.ARGUS_AGENT_TOKEN?.trim();
  if (!bootstrap && !agentToken) {
    log.warn(
      "ARGUS_URL is set but no ARGUS_BOOTSTRAP_TOKENS / ARGUS_AGENT_TOKEN; Argus channel disabled",
    );
    return null;
  }
  return createArgusAdapter({
    url,
    bootstrapToken: bootstrap || undefined,
    agentToken: agentToken || undefined,
    capabilities: (process.env.ARGUS_CAPABILITIES ?? "")
      .split(",")
      .map((c) => c.trim())
      .filter(Boolean),
    name: process.env.ARGUS_AGENT_NAME?.trim() || "nanoclaw",
  });
}

registerChannelAdapter(CHANNEL_TYPE, { factory: fromEnv, defaults: ARGUS_DEFAULTS });
