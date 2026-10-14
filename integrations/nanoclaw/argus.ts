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
 *   ARGUS_ACCOUNT_PROVIDERS   comma-separated providers (e.g. "anthropic"): turns on
 *                             account leasing from Argus and OneCLI switching;
 *                             without it the channel uses whatever credential
 *                             the OneCLI vault already has
 *   ONECLI_URL / ONECLI_API_KEY  how to reach the OneCLI vault (accounts only)
 *   ARGUS_EXIT_ON_DRAIN       "false" to keep the host running once Argus has
 *                             drained this agent (default: exit, so the
 *                             instance ends; only when ARGUS_INSTANCE_ID is set)
 */
import { log } from "../log.js";
import type { ChannelAdapter, ChannelDefaults, ChannelSetup, OutboundMessage } from "./adapter.js";
import {
  type AccountLease,
  type ApplyTarget,
  applyLease,
  classifyFailure,
  OneCliClient,
} from "./argus-accounts.js";
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
const MAX_RESENDS = 3; // account switches for one attempt before giving up

export interface ArgusConfig {
  url: string;
  bootstrapToken?: string;
  agentToken?: string;
  capabilities: string[];
  name: string;
  /** Account leasing from Argus into the OneCLI vault. Off when absent. */
  accounts?: {
    providers: string[];
    onecli: OneCliClient;
    /** Which OneCLI agent (the NanoClaw agent group) the credential is for. */
    resolveTarget: () => Promise<ApplyTarget>;
  };
  /** Called once, after Argus drained this agent, its work is done, and it left. */
  onDrained?: () => void;
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
  let currentText: string | null = null; // what the agent was sent, to re-send after a switch
  let resends = 0;
  let account: { id: string; name: string } | null = null;
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
      if (data.state === "draining") {
        draining = true;
        void completeDrainIfIdle();
      }
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

  /** Once drained and idle: leave Argus, stop claiming, and let the host exit. */
  let drainedHandled = false;
  async function completeDrainIfIdle(): Promise<void> {
    if (!draining || current || drainedHandled || !registered) return;
    drainedHandled = true;
    log.info("Argus drained this agent and it is idle; leaving");
    stopped = true;
    abortClaim?.abort();
    try {
      await client.call("POST", "/agents/me/leave");
    } catch (err) {
      log.warn("Argus leave failed (the reaper ends us if it persists)", { err });
    }
    registered = false;
    cfg.onDrained?.();
  }

  function finishTask(): void {
    current = null;
    currentText = null;
    pendingProgress = null;
    if (progressTimer) clearTimeout(progressTimer);
    progressTimer = null;
    const done = taskDone;
    taskDone = null;
    done?.();
    void completeDrainIfIdle();
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

  async function injectTask(taskId: string, attempt: number, text: string): Promise<void> {
    await setupCfg!.onInbound(PLATFORM_ID, null, {
      id: `argus-${taskId}-${attempt}${resends ? `-r${resends}` : ""}`,
      kind: "chat",
      timestamp: new Date().toISOString(),
      content: { text, sender: "argus", senderId: `argus:${PLATFORM_ID}` },
    });
  }

  /**
   * The agent's run failed. If the provider blocked it (rate limit / out of
   * credits) and accounts are managed, report the account exhausted and move to
   * the next one, then give the agent the task again; otherwise the attempt is
   * a failure and Argus decides whether to retry.
   */
  async function handleFailure(
    task: { id: string; attempt: number },
    noticeText: string,
  ): Promise<void> {
    const f = classifyFailure(noticeText);
    if (f.kind === "exhausted" && cfg.accounts && account) {
      const exhausted = account;
      account = null;
      const { data } = await client.call("POST", `/accounts/${exhausted.id}/exhausted`, {
        until: f.until,
        reason: f.reason,
        task_id: task.id,
        attempt: task.attempt,
      });
      log.warn("Argus account exhausted", { account: exhausted.name, until: f.until });
      if (data.lease) {
        const lease = data.lease as AccountLease;
        await applyLease(cfg.accounts.onecli, await cfg.accounts.resolveTarget(), lease);
        account = { id: lease.account_id, name: lease.name };
        log.info("Switched to another Argus account", { account: lease.name });
        if (currentText && resends < MAX_RESENDS) {
          resends += 1;
          await injectTask(
            task.id,
            task.attempt,
            `[Argus] The previous run hit a provider limit and the account was switched. ` +
              `Please continue this task.\n\n${currentText}`,
          );
          return;
        }
      }
      // No replacement (Argus blocked the task as no_account), or too many switches.
      if (!data.task_blocked) {
        await client.call("POST", `/tasks/${task.id}/fail`, {
          attempt: task.attempt,
          error: f.reason,
        });
      }
      finishTask();
      return;
    }
    await client.call("POST", `/tasks/${task.id}/fail`, {
      attempt: task.attempt,
      error: f.reason,
    });
    finishTask();
  }

  async function runTask(task: ArgusTask): Promise<void> {
    current = { id: task.id, attempt: task.attempt };
    lastProgressAt = 0;
    const finished = new Promise<void>((resolve) => {
      taskDone = resolve;
    });
    const pages = await inlinePages(task);
    log.info("Argus task claimed", { task: task.id, attempt: task.attempt });
    resends = 0;
    currentText = renderTask(task, pages);
    await injectTask(task.id, task.attempt, currentText);
    await finished;
  }

  /** Hold a leased account, applied to the vault, before claiming any work. */
  async function ensureAccount(): Promise<boolean> {
    const acc = cfg.accounts;
    if (!acc || account) return true;
    try {
      const { data } = await client.call("POST", "/accounts/lease", { providers: acc.providers });
      const lease = data as AccountLease;
      try {
        await applyLease(acc.onecli, await acc.resolveTarget(), lease);
      } catch (err) {
        // Do not sit on an account we cannot use: give it back, then retry later.
        await client.call("POST", "/accounts/release").catch(() => undefined);
        throw err;
      }
      account = { id: lease.account_id, name: lease.name };
      log.info("Argus account leased and applied", {
        account: lease.name,
        provider: lease.provider,
      });
      return true;
    } catch (err) {
      if (err instanceof ArgusError && err.code === "no_account") {
        log.warn("No Argus account is usable right now; not claiming work", {
          message: err.message,
        });
      } else {
        log.warn("Could not lease/apply an account (will retry)", { err });
      }
      await sleep(5000);
      return false;
    }
  }

  async function claimLoop(): Promise<void> {
    let backoff = 1000;
    while (!stopped) {
      if (draining) {
        await sleep(1000);
        continue;
      }
      if (!(await ensureAccount())) continue;
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
        if (err instanceof ArgusError && err.code === "agent_draining") {
          draining = true; // Argus told us directly: no need to wait for a heartbeat
          await completeDrainIfIdle();
          continue;
        }
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
      const isFailureNotice =
        typeof content === "object" && content !== null && content.failureNotice === true;
      const d = parseReply(text);
      try {
        if (isFailureNotice) {
          await handleFailure(task, text);
        } else if (d.kind === "complete") {
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

function accountsFromEnv(): ArgusConfig["accounts"] {
  const providers = (process.env.ARGUS_ACCOUNT_PROVIDERS ?? "")
    .split(",")
    .map((p) => p.trim())
    .filter(Boolean);
  if (providers.length === 0) return undefined;
  return {
    providers,
    onecli: new OneCliClient(
      process.env.ONECLI_URL?.trim() || "http://127.0.0.1:10254",
      fetch,
      process.env.ONECLI_API_KEY?.trim() || undefined,
    ),
    // The OneCLI agent NanoClaw uses is keyed by the agent group wired to this
    // channel, read from NanoClaw's own database when first needed.
    async resolveTarget(): Promise<ApplyTarget> {
      const { getMessagingGroupByPlatform, getMessagingGroupAgents } = await import(
        "../db/messaging-groups.js"
      );
      const { getAgentGroup } = await import("../db/agent-groups.js");
      const mg = await getMessagingGroupByPlatform(CHANNEL_TYPE, PLATFORM_ID);
      const wiring = mg ? (await getMessagingGroupAgents(mg.id))[0] : undefined;
      const group = wiring ? await getAgentGroup(wiring.agent_group_id) : undefined;
      if (!group) {
        throw new Error("no agent group is wired to the argus channel (run init-argus-agent.ts)");
      }
      return { identifier: group.id, name: group.name };
    },
  };
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
    accounts: accountsFromEnv(),
    // Under an Argus instance, a drained agent means this host should go: ask
    // NanoClaw to shut down through its own SIGTERM handling.
    onDrained:
      process.env.ARGUS_INSTANCE_ID && process.env.ARGUS_EXIT_ON_DRAIN !== "false"
        ? () => void process.kill(process.pid, "SIGTERM")
        : undefined,
  });
}

registerChannelAdapter(CHANNEL_TYPE, { factory: fromEnv, defaults: ARGUS_DEFAULTS });
