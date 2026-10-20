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
 *   ONECLI_URL / ONECLI_API_KEY  how to reach the OneCLI vault. Setting ONECLI_URL
 *                             turns on account leasing from Argus (the best usable
 *                             account across ALL providers, by priority) and OneCLI
 *                             switching; without it the channel uses whatever
 *                             credential the vault already has. The retired
 *                             ARGUS_ACCOUNT_PROVIDERS / ARGUS_EXECUTOR_PROVIDERS
 *                             are ignored (they never restrict selection).
 *   ARGUS_EXIT_ON_DRAIN       "false" to keep the host running once Argus has
 *                             drained this agent (default: exit, so the
 *                             instance ends; only when ARGUS_INSTANCE_ID is set)
 */
import { createHash } from "node:crypto";
import { readFileSync } from "node:fs";
import { mkdir, rename, writeFile } from "node:fs/promises";
import path from "node:path";
import { log } from "../log.js";
import type { ChannelAdapter, ChannelDefaults, ChannelSetup, OutboundMessage } from "./adapter.js";
import {
  type AccountLease,
  type ApplyTarget,
  applyLeases,
  applyLeasesDetailed,
  applyRepositoryLease,
  CURSOR_EXPIRY_MARGIN_MS,
  classifyFailure,
  classifyLogTail,
  classifyProviderFailure,
  executorFor,
  type FailureKind,
  isGenericFailureNotice,
  normalizeRepositoryUrl,
  OneCliClient,
  releaseRepositoryLease,
  runtimeProviders,
} from "./argus-accounts.js";
import { registerChannelAdapter } from "./channel-registry.js";
import {
  loadPolicy,
  type ModelSelection,
  policySnapshot,
  selectHarnessModel,
} from "./model-policy.mjs";

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

/** `TaskResult.summary` max_length, counted in characters (Unicode code points). */
export const SUMMARY_MAX_CHARS = 10_000;
const PROGRESS_MAX_CHARS = 2000;
const PROGRESS_MIN_INTERVAL_MS = 10_000; // Argus allows one progress note per 10 s
const CLAIM_WAIT_S = 20;
const MAX_PAGE_CHARS = 4000;
const MAX_PAGES_INLINED = 3;
const MAX_RESENDS = 3; // account switches for one attempt before giving up
const REBALANCE_MS = 60_000; // idle re-evaluation of the engine lease (priority recovery)
const RECYCLE_TIMEOUT_MS = 30_000; // wait for the old container to exit after an account rotation

/** The accounts held when a task's models are chosen (null: not leased). */
export interface HeldAccounts {
  engine: { provider: string; account_id: string } | null;
  executor: { provider: string; account_id: string } | null;
}

export interface ArgusConfig {
  url: string;
  bootstrapToken?: string;
  agentToken?: string;
  capabilities: string[];
  name: string;
  /** Account leasing from Argus into the OneCLI vault. Off when absent. */
  accounts?: {
    /**
     * Lease the engine's model account from Argus, which ranks every usable
     * account globally by priority. False: repository leases only.
     */
    models: boolean;
    /**
     * While idle (no task in flight) re-evaluate the engine lease at most this
     * often, so a recovered or newly added higher-priority account is adopted at
     * a safe point; active work is never rotated. Default 60 s; 0 disables.
     */
    rebalanceMs?: number;
    onecli: OneCliClient;
    /** Which OneCLI agent (the NanoClaw agent group) the credential is for. */
    resolveTarget: () => Promise<ApplyTarget>;
    /**
     * Run the worker's loop on the harness that fits the leased account (codex
     * accounts spec 3): called after a lease is applied, with the NanoClaw
     * provider name. Resolves true when the harness changed.
     */
    selectEngine?: (engine: string) => Promise<boolean>;
    /**
     * Stop the group's running session containers and resolve only once each has
     * exited, so the next one starts with the stubs of the account just applied.
     * Rejects when a container could not be stopped. Called on an account
     * rotation within one provider, where `selectEngine` stops nothing.
     */
    recycleRuntime?: () => Promise<void>;
    /** Explicit project -> HTTPS repository allowlist from this pool. */
    repositories?: Record<string, string>;
  };
  /** Apply a pinned policy before starting a task; absent only in test adapters. */
  selectModels?: (task: ArgusTask, held: HeldAccounts) => Promise<ModelSelection>;
  /**
   * The tail of the agent's log (host-provided). A run that ends with only the
   * generic failure notice is checked for Claude Code's `Rate limit [..] (resets
   * ..)` line. Absent: only the notice text and the structured providerFailure
   * can show an exhausted account.
   */
  agentLogTail?: () => Promise<string>;
  /** Called once, after Argus drained this agent, its work is done, and it left. */
  onDrained?: () => void;
  /** Reported on heartbeat as `runtime.worker_version`, e.g. "nanoclaw/1.2.3". */
  workerVersion?: string;
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

export interface ArgusTask {
  id: string;
  title: string;
  spec: string;
  project: string;
  required_capabilities?: string[];
  repository_url?: string;
  input?: Record<string, unknown>;
  attempt: number;
  failures?: number;
  error?: string | null;
  acceptance: string[];
  knowledge_refs: KnowledgeRef[];
}

/** True only when the task input explicitly sets delivery.allow_merge to boolean true. */
export function taskAuthorizesMerge(task: ArgusTask): boolean {
  const delivery = task.input?.delivery;
  if (!delivery || typeof delivery !== "object" || Array.isArray(delivery)) return false;
  return (delivery as Record<string, unknown>).allow_merge === true;
}

function repositoryTaskLines(task: ArgusTask): string[] {
  const lines = [
    `Repository (allowlisted): ${task.repository_url}`,
    "Use /usr/local/bin/argus-gitea-api for repository API access; do not handle credentials or add auth headers yourself. Commands: get = pull request by number; issue-get = issue tracker item; list-prs = paginated PR list with --state; status = commit statuses for the PR head SHA; create = open a pull request; update = edit a pull request.",
    `To open a PR (you can; push your branch first, then): argus-gitea-api create --repository ${task.repository_url} --title "<title>" --head <branch> --base <base-branch> --body "<text>"   (a long body may use --body-file <path> only if argus-gitea-api create accepts it on your image; an unsupported option is silently ignored, so check the returned PR body). create is idempotent: if an open PR for the same head and base exists it is returned instead of a duplicate. To edit one: argus-gitea-api update --repository ${task.repository_url} --number <N> [--title T] [--body-file PATH | --body TEXT] [--base B] [--state open|closed]. Both print the PR JSON (number, html_url, head.sha).`,
  ];
  if (taskAuthorizesMerge(task)) {
    lines.push(
      "This task explicitly authorizes merge when every precondition below holds; otherwise stop after CI/review without merging.",
      "Before merge: re-read the PR (`get`), confirm the current head SHA, and use `status` so every required check on that exact SHA is success (pending, failure, cancelled, or unknown is not approval; skipped is not execution). Resolve agentic review findings P1–P3. Respect repository branch protections and reviews. Never use force_merge, bypass, force-push, or mark a PR merged without a successful Gitea merge API call through the same allowlisted OneCLI-proxied HTTPS path as other argus-gitea-api operations (GiteaApi.request). If the head changes, re-run gates and CI on the new SHA before merging. After merge, read the PR back and record merged state and merge_commit_sha; a failed POST is not success.",
      "Merge does not authorize manual Production deploys, production data changes, or production flag toggles—only normal repository automation already configured.",
    );
  } else {
    lines.push(
      "Do not merge pull requests unless this task or its pinned project delivery policy (see spec, acceptance, and Relevant knowledge below) explicitly authorizes merge.",
    );
  }
  return lines;
}

export type Directive = { kind: "complete" | "fail" | "ask" | "progress"; text: string };

/** Preserve an explicit structured result envelope emitted by the worker. */
function completionPayload(text: string): { summary: string; data?: Record<string, unknown> } {
  try {
    const value: unknown = JSON.parse(text);
    if (value && typeof value === "object" && !Array.isArray(value)) {
      const record = value as Record<string, unknown>;
      if (typeof record.summary === "string") {
        return {
          summary: record.summary,
          ...(record.data && typeof record.data === "object" && !Array.isArray(record.data)
            ? { data: record.data as Record<string, unknown> }
            : {}),
        };
      }
    }
  } catch {
    // Plain-text completions retain their established behavior.
  }
  return { summary: text };
}

// Markdown decoration a model puts around a directive: quotes, bullets, bold/code.
const LEAD = "[\\s>*_`~#+-]*";
const COLON_DIRECTIVE = new RegExp(`^${LEAD}(complete|fail|ask)[*_\`~]*\\s*[:：][*_\`~]*\\s*`, "i");
// "FAIL - why": only the upper-case keyword, so prose like "Ask - then" stays progress.
const DASH_DIRECTIVE = new RegExp(`^${LEAD}(COMPLETE|FAIL|ASK)[*_\`~]*\\s+[-–—]\\s+`);

/**
 * Interpret an agent reply. A directive is read at the start of the first non-blank
 * line, case-insensitively, tolerating markdown around it (`**FAIL:**`, `> FAIL:`,
 * `- FAIL:`) and `KEYWORD - text`; the rest of the reply (further paragraphs
 * included) is its text.
 */
export function parseReply(raw: string): Directive {
  const text = raw.trim();
  const m = COLON_DIRECTIVE.exec(text) ?? DASH_DIRECTIVE.exec(text);
  if (!m) return { kind: "progress", text };
  return { kind: m[1]!.toLowerCase() as Directive["kind"], text: text.slice(m[0].length).trim() };
}

/** The message a claimed task becomes. `pages` are inlined knowledge bodies by page id. */
export function renderTask(
  task: ArgusTask,
  pages: Map<string, string>,
  preferredExecutor?: string | null,
): string {
  const lines = [
    `[Argus task] ${task.title}`,
    `Project: ${task.project} · Task: ${task.id} · Attempt: ${task.attempt}`,
    ...(task.repository_url ? repositoryTaskLines(task) : []),
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
    "Delegated implementation (mandatory): run all coding/model work through /usr/local/bin/argus-delegate EXECUTOR.",
    "EXECUTOR is the leased CLI role (claude | codex | opencode | agent). When Cursor is the selected coding executor, use EXECUTOR agent.",
    ...(preferredExecutor
      ? [
          `Preferred EXECUTOR for this task: ${preferredExecutor} (the CLI of the best-priority account Argus leased to this worker).`,
        ]
      : []),
    "Do not invoke agent, cursor-agent, claude, codex, or opencode directly for implementation — that bypasses",
    "prepareExecutorEnv (writable private Cursor config), model policy, timeouts, and the JSON delegate report.",
    "Direct Git, gates, QA, and other non-delegated shell commands in the checkout are fine when they are not a",
    "substitute for argus-delegate.",
    "",
    "Example (normal implementation, not --read-only):",
    "  /usr/local/bin/argus-delegate agent \\",
    "    --cwd <task-checkout> --prompt-file <prompt.txt> --out <task-checkout>/.argus-delegate --timeout <seconds>",
    "Use the model and retry policy from argus-model-policy.json (or --model / --model-attempt when retrying).",
    "",
    "The delegate wrapper may materialize gateway stubs and cli-config metadata in a private writable directory;",
    "that is authorized and is not a copy of real credentials. Do not chmod or remount read-only seed files, and",
    "do not read auth.json or other secret material.",
    "",
    "Model policy: argus-model-policy.json pins this task policy for argus-delegate. Attach the delegate JSON",
    "report (last stdout line) as a task artifact. After a failed verification/context limit, retry with",
    "--model-attempt N --fallback-reason REASON; --model pins an override.",
    "",
    "If delegation fails, resume this same task (same Task id above) with the error and artifact context; do not",
    "treat another task's block or missing Postgres as proof this task cannot proceed.",
    "",
    "How to respond: send short progress updates as normal messages. When you are done, reply with a",
    'message starting with "COMPLETE:" followed by a summary of what you did. If you cannot do it,',
    'start with "FAIL:" and say why. If you need someone to decide something, start with "ASK:" and',
    "ask the question; you will be given the answer.",
  );
  return lines.join("\n");
}

export class ArgusError extends Error {
  constructor(
    readonly status: number,
    readonly code: string,
    message: string,
  ) {
    super(message);
  }

  /** A rejection retrying cannot fix (invalid input): 4xx other than auth, timeout, conflict, rate limit. */
  get permanent(): boolean {
    return (
      this.status >= 400 && this.status < 500 && ![401, 408, 409, 425, 429].includes(this.status)
    );
  }
}

/**
 * The server's real error, whatever shape it has: `{detail:{code,message}}` (domain
 * errors), `{detail:[{loc,msg}]}` (request validation), `{detail:"text"}`, or no JSON
 * body at all. Never `[object Object]`.
 */
export function describeError(
  data: unknown,
  statusText: string,
  raw = "",
): { code: string; message: string } {
  const detail = (data as { detail?: unknown } | null)?.detail;
  if (Array.isArray(detail)) {
    const parts = detail.map((e) => {
      const loc = Array.isArray(e?.loc) ? e.loc.slice(1).join(".") : "";
      return `${loc ? `${loc}: ` : ""}${e?.msg ?? JSON.stringify(e)}`;
    });
    return { code: "validation_failed", message: parts.join("; ") || statusText };
  }
  if (detail && typeof detail === "object") {
    const d = detail as { code?: unknown; message?: unknown };
    return {
      code: typeof d.code === "string" ? d.code : "error",
      message: typeof d.message === "string" ? d.message : JSON.stringify(detail).slice(0, 300),
    };
  }
  if (typeof detail === "string") return { code: "error", message: detail };
  return { code: "error", message: raw.trim().slice(0, 200) || statusText };
}

class ArgusClient {
  token = "";
  constructor(private readonly cfg: ArgusConfig) {}

  private async send(
    method: string,
    path: string,
    init: { body?: RequestInit["body"]; contentType: string; signal?: AbortSignal },
  ): Promise<{ status: number; data: any }> {
    const doFetch = this.cfg.fetch ?? fetch;
    const res = await doFetch(this.cfg.url.replace(/\/$/, "") + "/api" + path, {
      method,
      headers: {
        "Content-Type": init.contentType,
        ...(this.token ? { Authorization: `Bearer ${this.token}` } : {}),
      },
      body: init.body,
      signal: init.signal,
    });
    const text = await res.text();
    let data: any = null;
    try {
      data = text ? JSON.parse(text) : null;
    } catch {
      data = null; // a proxy's HTML error page, say: handled below by status
    }
    if (res.status >= 400) {
      const { code, message } = describeError(data, res.statusText, text);
      throw new ArgusError(res.status, code, message);
    }
    return { status: res.status, data };
  }

  call(
    method: string,
    path: string,
    body?: unknown,
    signal?: AbortSignal,
  ): Promise<{ status: number; data: any }> {
    return this.send(method, path, {
      body: body === undefined ? undefined : JSON.stringify(body),
      contentType: "application/json",
      signal,
    });
  }

  /** Upload raw bytes as an artifact of the attempt this agent holds. */
  upload(taskId: string, attempt: number, name: string, bytes: Uint8Array, mediaType: string) {
    const q = `attempt=${attempt}&name=${encodeURIComponent(name)}`;
    return this.send("POST", `/tasks/${taskId}/artifacts?${q}`, {
      body: bytes as unknown as RequestInit["body"],
      contentType: mediaType,
    });
  }
}

const chars = (s: string): string[] => Array.from(s);

/**
 * The part of `text` that fits `budget` characters, cut back to a line break when
 * one is near the end (so the summary does not stop mid-sentence), by code point.
 */
/** An Argus artifact name from an agent's file name: [A-Za-z0-9._-], starting alphanumeric, at most 200. */
export function artifactName(filename: string): string {
  const base = filename.split(/[\\/]/).pop() ?? "";
  const clean = base
    .normalize("NFKD")
    .replace(/\p{M}+/gu, "")
    .replace(/[^A-Za-z0-9._-]+/g, "-")
    .replace(/^[._-]+/, "");
  return cutKeepingExtension(clean, 200) || "attachment";
}

/** `name` cut to `max` characters in its stem, so a (short) extension always survives. */
function cutKeepingExtension(name: string, max: number): string {
  if (name.length <= max) return name;
  const { stem, ext } = splitExtension(name);
  return stem.slice(0, max - ext.length) + ext;
}

/** The extension (with its dot) when there is a sensible one: not leading, at most 20 characters. */
function splitExtension(name: string): { stem: string; ext: string } {
  const dot = name.lastIndexOf(".");
  const ext = dot > 0 && name.length - dot <= 20 ? name.slice(dot) : "";
  return { stem: ext ? name.slice(0, dot) : name, ext };
}

/**
 * `name` with `-<sha8>` before its extension (or at the end when it has none), still a
 * valid artifact name: at most 200 characters, so a long stem is cut, never the extension.
 */
export function withHashSuffix(name: string, sha: string): string {
  const suffix = `-${sha.slice(0, 8)}`;
  const { stem, ext } = splitExtension(name);
  return stem.slice(0, 200 - suffix.length - ext.length) + suffix + ext;
}

const MEDIA_TYPES: Record<string, string> = {
  md: "text/markdown; charset=utf-8",
  txt: "text/plain; charset=utf-8",
  json: "application/json",
  csv: "text/csv; charset=utf-8",
  html: "text/html; charset=utf-8",
  pdf: "application/pdf",
  png: "image/png",
};
export const mediaTypeFor = (name: string): string =>
  MEDIA_TYPES[name.split(".").pop()?.toLowerCase() ?? ""] ?? "application/octet-stream";

export function headWithin(text: string, budget: number): string {
  const cps = chars(text);
  if (cps.length <= budget) return text;
  let head = cps.slice(0, budget).join("");
  const nl = head.lastIndexOf("\n");
  if (nl > head.length * 0.8) head = head.slice(0, nl);
  return head.trimEnd();
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
  let currentClaim: ArgusTask | null = null;
  let currentText: string | null = null; // what the agent was sent, to re-send after a switch
  // Artifacts uploaded for the task in flight (agent attachments), by artifact id,
  // and attachments that could not be stored, for the completion to report.
  let attached = new Map<string, { name: string; sha: string }>();
  let attachFailures: string[] = [];
  let resends = 0;
  let account: { id: string; name: string; provider: string } | null = null;
  let executor: { id: string; name: string; provider: string } | null = null;
  let leasedAt = 0; // when the engine lease was last confirmed (idle rebalance)
  let recycleOnLease = false; // Argus moved us to another account: recycle once the next one is applied
  let engineInUse: string | null = null; // the harness the leased account selected
  let activeAccountLeases: AccountLease[] = [];
  let repositoryTarget: ApplyTarget | null = null;
  let repositoryLeaseHeld = false;
  let activeRepositoryLease: AccountLease | null = null;
  let activeRepositoryUrl: string | null = null;
  let activeRepositoryAllowlist: string[] = [];
  let repositoryAssignmentReconciled = false;
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
        // Self-reported, shown by pool inspection as such; null means not known.
        runtime: {
          worker_version: cfg.workerVersion ?? null,
          engine: engineInUse,
          executor: executor?.provider ?? null,
        },
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
    currentClaim = null;
    currentText = null;
    attached = new Map();
    attachFailures = [];
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
    providerFailure?: unknown,
  ): Promise<void> {
    const textual = classifyFailure(noticeText, account?.provider);
    let f: FailureKind =
      textual.kind === "failed"
        ? (classifyProviderFailure(providerFailure, account?.provider) ?? textual)
        : textual;
    if (f.kind === "failed" && isGenericFailureNotice(noticeText) && cfg.agentLogTail) {
      try {
        f = classifyLogTail(await cfg.agentLogTail(), account?.provider) ?? f;
      } catch (err) {
        log.warn("Could not read the agent log tail", { err });
      }
    }
    if (f.kind === "exhausted" && cfg.accounts && account) {
      const exhausted = account;
      account = null;
      const { data } = await client.call("POST", `/accounts/${exhausted.id}/exhausted`, {
        until: f.until,
        reason: f.reason,
        role: "engine",
        runtime_providers: runtimeProviders("engine"),
        task_id: task.id,
        attempt: task.attempt,
      });
      log.warn("Argus account exhausted", { account: exhausted.name, until: f.until });
      const next = data.lease as AccountLease | undefined;
      log.warn(
        `account_failover ${JSON.stringify({
          from: exhausted.name,
          to: next?.name ?? null,
          reason: f.reason,
          task_id: task.id,
          attempt: task.attempt,
        })}`,
      );
      if (data.lease) {
        const lease = data.lease as AccountLease;
        await applyAndSelect(cfg.accounts, lease, "exhausted");
        account = { id: lease.account_id, name: lease.name, provider: lease.provider };
        leasedAt = cfg.now?.() ?? Date.now();
        try {
          // The old container holds the previous account's stubs: it must be gone
          // before the task is sent again.
          await cfg.accounts.recycleRuntime?.();
        } catch (err) {
          log.error("The previous runtime could not be stopped; not resending the task", { err });
          // A quota-led reason makes Argus requeue without consuming the attempt.
          await client.call("POST", `/tasks/${task.id}/fail`, {
            attempt: task.attempt,
            error: `${f.reason}; the account was switched but the previous worker runtime could not be stopped`,
            reason: "provider_failure",
          });
          finishTask();
          return;
        }
        log.info("Switched to another Argus account", { account: lease.name });
        if (currentClaim) await prepareModels(currentClaim);
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
        // With no replacement account the limit is not the task's fault: Argus
        // requeues it without consuming an attempt and holds claims until an
        // account refills.
        await client.call("POST", `/tasks/${task.id}/fail`, {
          attempt: task.attempt,
          error: f.reason,
          reason: data.lease ? "provider_failure" : "quota",
        });
      }
      finishTask();
      return;
    }
    const { data: failed } = await client.call("POST", `/tasks/${task.id}/fail`, {
      attempt: task.attempt,
      error: f.reason,
      reason: "provider_failure",
    });
    // Argus may have recognised a quota notice itself and moved this worker to
    // another account (account selection 3.6): drop the stale one; the next lease
    // is applied and the runtime recycled.
    const switched = (failed as { failover?: { attempt?: number } } | null)?.failover;
    if (switched?.attempt === task.attempt && account) {
      account = null;
      recycleOnLease = true;
    }
    finishTask();
  }

  async function prepareModels(task: ArgusTask): Promise<void> {
    if (!cfg.selectModels) return;
    const selection = await cfg.selectModels(task, {
      engine: account ? { provider: account.provider, account_id: account.id } : null,
      executor: executor ? { provider: executor.provider, account_id: executor.id } : null,
    });
    const bytes = new TextEncoder().encode(JSON.stringify(selection, null, 2));
    const sha = createHash("sha256").update(bytes).digest("hex");
    const name = `model-policy-attempt-${task.attempt}.json`;
    attached.set(await ensureArtifact(task, name, bytes, sha, "application/json"), { name, sha });
  }

  async function runTask(task: ArgusTask): Promise<void> {
    current = { id: task.id, attempt: task.attempt };
    lastProgressAt = 0;
    const finished = new Promise<void>((resolve) => {
      taskDone = resolve;
    });
    currentClaim = task;
    try {
      await prepareModels(task);
    } catch (err) {
      await client.call("POST", `/tasks/${task.id}/fail`, {
        attempt: task.attempt,
        error: `model policy could not be applied/recorded: ${String(err)}`,
        reason: "channel_error",
      });
      finishTask();
      return;
    }
    try {
      if (task.required_capabilities?.includes("repository:write")) {
        const acc = cfg.accounts;
        const repositoryUrl = acc?.repositories?.[task.project];
        if (!acc || !repositoryUrl)
          throw new Error("repository is not configured for this task project");
        if (activeAccountLeases.length === 0)
          throw new Error("cannot safely reconcile repository access without active base leases");
        const normalizedRepository = normalizeRepositoryUrl(repositoryUrl);
        const { data } = await client.call("POST", "/accounts/lease", {
          role: "repository",
        });
        const lease = data as AccountLease;
        repositoryLeaseHeld = true;
        activeRepositoryLease = lease;
        activeRepositoryUrl = normalizedRepository.cloneUrl;
        activeRepositoryAllowlist = Object.values(acc.repositories ?? {});
        repositoryTarget = await acc.resolveTarget();
        await applyRepositoryLease(
          acc.onecli,
          repositoryTarget,
          lease,
          normalizedRepository.cloneUrl,
          activeRepositoryAllowlist,
          activeAccountLeases,
        );
        task.repository_url = normalizedRepository.cloneUrl;
      }
      const pages = await inlinePages(task);
      log.info("Argus task claimed", { task: task.id, attempt: task.attempt });
      resends = 0;
      const best = executor ?? account;
      currentText = renderTask(task, pages, best ? executorFor(best.provider) : null);
      await injectTask(task.id, task.attempt, currentText);
      await finished;
    } catch {
      await client
        .call("POST", `/tasks/${task.id}/fail`, {
          attempt: task.attempt,
          error: "worker repository credential unavailable or repository is not allowlisted",
          reason: "channel_error",
        })
        .catch(() => undefined);
      finishTask();
    } finally {
      if (repositoryLeaseHeld && cfg.accounts) {
        repositoryLeaseHeld = false;
        activeRepositoryLease = null;
        activeRepositoryUrl = null;
        activeRepositoryAllowlist = [];
        if (repositoryTarget) {
          try {
            await releaseRepositoryLease(
              cfg.accounts.onecli,
              repositoryTarget,
              activeAccountLeases,
            );
            repositoryAssignmentReconciled = true;
          } catch {
            repositoryAssignmentReconciled = false;
            log.warn("Could not remove repository credential assignment; task claiming is blocked");
          }
          repositoryTarget = null;
        }
        await client.call("POST", "/accounts/release?role=repository").catch(() => undefined);
      }
    }
  }

  /**
   * Apply the engine lease, plus an executor lease when the engine is of another
   * provider than the executor's, so the vault holds exactly the accounts the
   * agent holds. Then move the harness to the engine's provider and report it.
   */
  async function applyAndSelect(
    acc: NonNullable<ArgusConfig["accounts"]>,
    lease: AccountLease,
    reason: "lease" | "exhausted",
  ): Promise<void> {
    const held: AccountLease[] = [lease];
    // The executor (a delegated CLI) is the best usable account across providers
    // too. It is held only when it is of another provider than the engine's: the
    // vault keeps one account per provider, and the engine's serves the CLI of its
    // own provider.
    try {
      const { data } = await client.call("POST", "/accounts/lease", {
        role: "executor",
        runtime_providers: runtimeProviders("executor"),
        // The engine's own credential already serves its provider's CLI: if that
        // account is the best executor too, Argus returns it and takes no slot.
        reuse_held: true,
      });
      const candidate = data as AccountLease;
      if (candidate.role === "executor" && candidate.provider !== lease.provider)
        held.push(candidate);
    } catch (err) {
      if (!(err instanceof ArgusError && err.code === "no_account")) throw err;
      log.warn("No executor account is usable; delegation to a CLI will be blocked", {
        message: err.message,
      });
    }
    if (held.length === 1) {
      // Not needed (or none available): do not keep an executor lease around.
      await client.call("POST", "/accounts/release?role=executor").catch(() => undefined);
    }
    const target = await acc.resolveTarget();
    let engine: string;
    const fetchImpl = cfg.fetch ?? fetch;
    try {
      const applied = await applyLeasesDetailed(acc.onecli, target, held, fetchImpl);
      engine = applied.engine;
      if (
        applied.executorExpiresAt !== null &&
        applied.executorExpiresAt <= (cfg.now?.() ?? Date.now()) + CURSOR_EXPIRY_MARGIN_MS
      ) {
        log.warn(
          "The Cursor executor access token is expired or about to expire; renew the account in Argus (the channel does not refresh OAuth tokens)",
          { account: held[1]?.name, expires_at: applied.executorExpiresAt },
        );
      }
    } catch (err) {
      if (held.length < 2) throw err;
      // An unusable executor account (expired token, no credential field) must
      // not take the engine down with it: serve without it and say so.
      log.warn("The executor account cannot be applied; continuing without it", {
        account: held[1]!.name,
        message: err instanceof Error ? err.message : String(err),
      });
      held.pop();
      await client.call("POST", "/accounts/release?role=executor").catch(() => undefined);
      engine = (await applyLeasesDetailed(acc.onecli, target, held, fetchImpl)).engine;
    }
    executor = held[1]
      ? { id: held[1].account_id, name: held[1].name, provider: held[1].provider }
      : null;
    activeAccountLeases = [...held];
    if (activeRepositoryLease && activeRepositoryUrl) {
      await applyRepositoryLease(
        acc.onecli,
        target,
        activeRepositoryLease,
        activeRepositoryUrl,
        activeRepositoryAllowlist,
        activeAccountLeases,
      );
    }
    repositoryAssignmentReconciled = true;
    engineInUse = engine;
    const changed = (await acc.selectEngine?.(engine)) ?? false;
    if (changed) log.info("Engine selected for the leased account", { engine, reason });
    // Best effort: the audit event must not block work.
    await client
      .call("POST", "/engines/selected", {
        engine,
        provider: lease.provider,
        account_id: lease.account_id,
        reason,
      })
      .catch((err) => log.warn("Could not report the engine selection", { err }));
  }

  /** Hold a leased account, applied to the vault, before claiming any work. */
  async function ensureAccount(): Promise<boolean> {
    const acc = cfg.accounts;
    if (!acc) return true;
    if (!repositoryAssignmentReconciled && activeAccountLeases.length > 0) {
      try {
        await releaseRepositoryLease(acc.onecli, await acc.resolveTarget(), activeAccountLeases);
        repositoryAssignmentReconciled = true;
      } catch {
        log.warn("Could not reconcile a stale repository credential assignment");
        await sleep(5000);
        return false;
      }
    }
    if (!acc.models) return true;
    const rebalancing = account !== null;
    if (rebalancing) {
      const every = acc.rebalanceMs ?? REBALANCE_MS;
      if (every <= 0 || (cfg.now?.() ?? Date.now()) - leasedAt < every) return true;
      leasedAt = cfg.now?.() ?? Date.now();
    }
    try {
      const { data } = await client.call("POST", "/accounts/lease", {
        runtime_providers: runtimeProviders("engine"),
      });
      const lease = data as AccountLease;
      if (rebalancing && lease.account_id === account?.id) return true; // still the best
      try {
        await applyAndSelect(acc, lease, "lease");
        if (rebalancing || recycleOnLease) await acc.recycleRuntime?.();
        recycleOnLease = false;
      } catch (err) {
        // Do not sit on an account we cannot use: give it back, then retry later.
        await client.call("POST", "/accounts/release").catch(() => undefined);
        account = null;
        throw err;
      }
      account = { id: lease.account_id, name: lease.name, provider: lease.provider };
      leasedAt = cfg.now?.() ?? Date.now();
      log.info("Argus account leased and applied", {
        account: lease.name,
        provider: lease.provider,
      });
      return true;
    } catch (err) {
      if (rebalancing && account) {
        // The held lease is still valid (a failed lease rolls back); keep working.
        log.warn("Could not re-evaluate the account lease; keeping the current one", { err });
        return true;
      }
      if (err instanceof ArgusError && err.code === "no_account") {
        log.warn("No Argus account is usable right now; not claiming work", {
          message: err.message,
        });
        // Ready work this worker could take is shown as awaiting quota, with when.
        await client
          .call("POST", "/accounts/awaiting-quota", {
            capabilities: cfg.capabilities,
            runtime_providers: runtimeProviders("engine"),
          })
          .catch((e) => log.warn("Could not mark ready tasks as awaiting quota", { err: e }));
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

  /**
   * Complete with `text`. Within the summary limit it is the summary; beyond it the
   * whole text is attached as an artifact and the summary is a bounded head that says
   * so. Nothing is truncated silently, and a retry reuses the artifact it uploaded.
   */
  async function completeWithResult(
    t: { id: string; attempt: number },
    text: string,
    data?: Record<string, unknown>,
  ): Promise<void> {
    const extra = [...attached.keys()];
    const missing = attachFailures.length ? `\n\n[Not stored: ${attachFailures.join("; ")}]` : "";
    if (chars(text + missing).length <= SUMMARY_MAX_CHARS) {
      await client.call("POST", `/tasks/${t.id}/complete`, {
        attempt: t.attempt,
        result: {
          summary: text + missing,
          ...(data ? { data } : {}),
          ...(extra.length ? { artifacts: extra } : {}),
        },
      });
      return;
    }
    const bytes = new TextEncoder().encode(text);
    const sha = createHash("sha256").update(bytes).digest("hex");
    const artifactId = await ensureArtifact(t, `result-attempt-${t.attempt}.md`, bytes, sha);
    const note =
      `\n\n[The full response (${chars(text).length} characters, ${bytes.length} bytes, ` +
      `sha256 ${sha.slice(0, 16)}…) is attached as artifact ${artifactId}.]`;
    const tail = note + missing;
    const summary = `${headWithin(text, SUMMARY_MAX_CHARS - chars(tail).length - 1)}…${tail}`;
    await client.call("POST", `/tasks/${t.id}/complete`, {
      attempt: t.attempt,
      result: {
        summary,
        ...(data ? { data } : {}),
        artifacts: [...new Set([artifactId, ...extra])],
      },
    });
  }

  async function ensureArtifact(
    t: { id: string; attempt: number },
    name: string,
    bytes: Uint8Array,
    sha: string,
    mediaType = "text/markdown; charset=utf-8",
  ): Promise<string> {
    // Artifact names are unique per (task, attempt) and immutable: a name that is
    // taken is reused only if it holds these very bytes, else a hashed name is used.
    const store = async (n: string): Promise<{ id: string; stored: boolean }> => {
      try {
        return {
          id: (await client.upload(t.id, t.attempt, n, bytes, mediaType)).data.id,
          stored: true,
        };
      } catch (err) {
        if (!(err instanceof ArgusError) || err.status !== 409 || err.code !== "artifact_exists") {
          throw err;
        }
        const { data } = await client.call("GET", `/tasks/${t.id}/artifacts`);
        const have = (
          data as Array<{ id: string; name: string; attempt: number; sha256: string }>
        ).find((a) => a.attempt === t.attempt && a.name === n);
        if (!have) throw err;
        if (have.sha256 !== sha) return { id: "", stored: false };
        return { id: have.id, stored: false };
      }
    };
    const first = await store(name);
    if (first.id) return first.id;
    const other = withHashSuffix(name, sha);
    const second = await store(other);
    if (second.id) return second.id;
    throw new ArgusError(
      422,
      "artifact_name_taken",
      `artifact ${other} already exists for this attempt with different content`,
    );
  }

  /** Upload the files the agent attached to a reply as artifacts of the attempt. */
  async function attachFiles(
    t: { id: string; attempt: number },
    files: Array<{ filename: string; data: Buffer | Uint8Array }>,
  ): Promise<void> {
    for (const f of files) {
      const bytes = new Uint8Array(f.data);
      const sha = createHash("sha256").update(bytes).digest("hex");
      let name = artifactName(f.filename);
      const same = [...attached.values()].find((a) => a.name === name);
      if (same?.sha === sha) continue; // this very file was already stored (a re-delivery)
      if (same) name = withHashSuffix(name, sha);
      try {
        attached.set(await ensureArtifact(t, name, bytes, sha, mediaTypeFor(name)), { name, sha });
      } catch (err) {
        if (!(err instanceof ArgusError) || !err.permanent) throw err; // transient: retried
        log.error("Argus refused an attachment", {
          name,
          status: err.status,
          message: err.message,
        });
        attachFailures.push(`${name} (${err.status} ${err.code}: ${err.message})`.slice(0, 300));
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
        message: chars(text).slice(0, PROGRESS_MAX_CHARS).join(""),
      });
    } catch (err) {
      if (err instanceof ArgusError && err.status === 429)
        pendingProgress = text; // retry on the timer
      else
        log.warn("Argus progress note failed", {
          permanent: err instanceof ArgusError ? err.permanent : false,
          err: err instanceof Error ? err.message : String(err),
        });
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
      if (task && message.files?.length) await attachFiles(task, message.files);
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
          await handleFailure(task, text, (content as Record<string, unknown>).providerFailure);
        } else if (d.kind === "complete") {
          const result = completionPayload(d.text);
          await completeWithResult(task, result.summary || "(no summary)", result.data);
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
            directive: d.kind,
            // A dropped FAIL is the one reply whose text is worth keeping in the log.
            ...(d.kind === "fail" ? { text: d.text.slice(0, 500) } : {}),
          });
          finishTask();
        } else if (err instanceof ArgusError && err.permanent) {
          // Retrying cannot fix it: say so on the task instead of leaving it claimed forever.
          log.error("Argus rejected the reply permanently; failing the attempt", {
            task: task.id,
            status: err.status,
            code: err.code,
            message: err.message,
          });
          await client.call("POST", `/tasks/${task.id}/fail`, {
            attempt: task.attempt,
            error:
              `The worker's reply could not be delivered (${err.status} ${err.code}): ${err.message}`.slice(
                0,
                2000,
              ),
            reason: "channel_error",
          });
          finishTask();
        } else {
          throw err; // transient (network, 5xx, 429): the delivery retry path handles it
        }
      }
      return undefined;
    },
  };
  return adapter;
}

export function accountsFromEnv(): ArgusConfig["accounts"] {
  // Account leasing is on when a vault is configured (the fleet always sets
  // ONECLI_URL). The retired provider lists only signal "on" for pools that still
  // carry them; they never restrict which accounts may be leased.
  const retired = ["ARGUS_ACCOUNT_PROVIDERS", "ARGUS_EXECUTOR_PROVIDERS"].filter((name) =>
    process.env[name]?.trim(),
  );
  if (retired.length > 0) {
    log.warn("Ignoring retired provider lists; accounts are chosen globally by priority", {
      retired,
    });
  }
  const models = !!process.env.ONECLI_URL?.trim() || !!process.env.ARGUS_ACCOUNT_PROVIDERS?.trim();
  let repositories: Record<string, string> = {};
  const repositoriesJson = process.env.ARGUS_REPOSITORIES_JSON?.trim();
  if (repositoriesJson) {
    try {
      const parsed: unknown = JSON.parse(repositoriesJson);
      if (parsed && typeof parsed === "object" && !Array.isArray(parsed)) {
        repositories = Object.fromEntries(
          Object.entries(parsed).filter(
            (entry): entry is [string, string] => typeof entry[1] === "string",
          ),
        );
      }
    } catch {
      throw new Error("ARGUS_REPOSITORIES_JSON must be a JSON project-to-URL object");
    }
  }
  if (!models && Object.keys(repositories).length === 0) return undefined;
  // The OneCLI agent NanoClaw uses is keyed by the agent group wired to this
  // channel, read from NanoClaw's own database when first needed.
  async function resolveTarget(): Promise<ApplyTarget> {
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
  }
  return {
    models,
    repositories,
    onecli: new OneCliClient(
      process.env.ONECLI_URL?.trim() || "http://127.0.0.1:10254",
      fetch,
      process.env.ONECLI_API_KEY?.trim() || undefined,
    ),
    resolveTarget,
    // Follow the leased account's provider: the group's config and its sessions'
    // pinned provider both name the harness, and a running container is stopped
    // so the next turn starts on the new one.
    async selectEngine(engine: string): Promise<boolean> {
      const { identifier: groupId } = await resolveTarget();
      const { getContainerConfig, updateContainerConfigScalars } = await import(
        "../db/container-configs.js"
      );
      const { getSessionsByAgentGroup, updateSession } = await import("../db/sessions.js");
      const { killContainer } = await import("../container-runner.js");
      let changed = false;
      const config = await getContainerConfig(groupId);
      if (!config) throw new Error(`agent group ${groupId} has no container config`);
      if ((config.provider ?? "claude").toLowerCase() !== engine) {
        await updateContainerConfigScalars(groupId, { provider: engine });
        changed = true;
      }
      for (const session of await getSessionsByAgentGroup(groupId)) {
        const current = (session.agent_provider || config.provider || "claude").toLowerCase();
        if (current === engine && session.agent_provider === engine) continue;
        await updateSession(session.id, { agent_provider: engine });
        if (current !== engine) {
          killContainer(session.id, "argus: engine follows the leased account");
          changed = true;
        }
      }
      return changed;
    },
    async recycleRuntime(): Promise<void> {
      const { identifier: groupId } = await resolveTarget();
      const { getSessionsByAgentGroup } = await import("../db/sessions.js");
      const { isContainerRunning, killContainer } = await import("../container-runner.js");
      const stopped = (await getSessionsByAgentGroup(groupId))
        .filter((session) => isContainerRunning(session.id))
        .map(
          (session) =>
            new Promise<void>((resolve, reject) => {
              // onExit fires only after a complete teardown, so a failed stop
              // never resolves and the timeout turns it into a rejection.
              const timer = setTimeout(
                () => reject(new Error(`session ${session.id} did not stop`)),
                RECYCLE_TIMEOUT_MS,
              );
              killContainer(session.id, "argus: account rotated", () => {
                clearTimeout(timer);
                resolve();
              });
            }),
        );
      await Promise.all(stopped);
    },
  };
}

export function modelsFromEnv(): NonNullable<ArgusConfig["selectModels"]> {
  // Validate once at host startup. A pool update cannot mutate an in-flight policy.
  const snapshot = loadPolicy();
  return async (task, held = { engine: null, executor: null }) => {
    const { getMessagingGroupByPlatform, getMessagingGroupAgents } = await import(
      "../db/messaging-groups.js"
    );
    const { getAgentGroup } = await import("../db/agent-groups.js");
    const { getContainerConfig, updateContainerConfigScalars } = await import(
      "../db/container-configs.js"
    );
    const { getSessionsByAgentGroup } = await import("../db/sessions.js");
    const { killContainer } = await import("../container-runner.js");
    const mg = await getMessagingGroupByPlatform(CHANNEL_TYPE, PLATFORM_ID);
    const wiring = mg ? (await getMessagingGroupAgents(mg.id))[0] : undefined;
    const group = wiring ? await getAgentGroup(wiring.agent_group_id) : undefined;
    if (!group) throw new Error("no Argus group for model policy");
    const config = await getContainerConfig(group.id);
    if (!config) throw new Error("no container config for model policy");
    const engine = (config.provider || "claude").toLowerCase();
    const selection = selectHarnessModel(snapshot, engine, task);
    if (held.engine && !selection.configured_model) {
      // Never leave the previous engine's model configured for the leased account's.
      throw new Error(`model policy has no harness chain for engine ${engine}`);
    }
    if (selection.configured_model && config.model !== selection.configured_model) {
      await updateContainerConfigScalars(group.id, { model: selection.configured_model });
      for (const session of await getSessionsByAgentGroup(group.id)) {
        killContainer(session.id, "argus: model policy changed");
      }
    }
    const pinned = policySnapshot(snapshot.policy, {
      task_id: task.id,
      attempt: task.attempt,
      engine,
      account_provider: held.engine?.provider ?? null,
      executor_account_provider: held.executor?.provider ?? held.engine?.provider ?? null,
      // Opaque lease ids (no credential): the delegate reports executor usage against them.
      engine_account_id: held.engine?.account_id ?? null,
      executor_account_id: (held.executor ?? held.engine)?.account_id ?? null,
      preferred_executor: executorFor(held.executor?.provider ?? held.engine?.provider ?? ""),
    });
    const dir = path.resolve("groups", group.folder);
    await mkdir(dir, { recursive: true });
    const file = path.join(dir, "argus-model-policy.json");
    await writeFile(file + ".tmp", JSON.stringify(pinned) + "\n");
    await rename(file + ".tmp", file);
    return {
      ...selection,
      configured_model: selection.configured_model ?? config.model ?? null,
      policy: snapshot.policy,
    };
  };
}

/** The running NanoClaw's version, from its package.json (the host's cwd). */
function nanoclawVersion(): string | undefined {
  try {
    const pkg = JSON.parse(readFileSync(path.join(process.cwd(), "package.json"), "utf8"));
    return typeof pkg.version === "string" ? `nanoclaw/${pkg.version}` : undefined;
  } catch {
    return undefined;
  }
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
    workerVersion: nanoclawVersion(),
    accounts: accountsFromEnv(),
    selectModels: modelsFromEnv(),
    // Under an Argus instance, a drained agent means this host should go: ask
    // NanoClaw to shut down through its own SIGTERM handling.
    onDrained:
      process.env.ARGUS_INSTANCE_ID && process.env.ARGUS_EXIT_ON_DRAIN !== "false"
        ? () => void process.kill(process.pid, "SIGTERM")
        : undefined,
  });
}

registerChannelAdapter(CHANNEL_TYPE, { factory: fromEnv, defaults: ARGUS_DEFAULTS });
