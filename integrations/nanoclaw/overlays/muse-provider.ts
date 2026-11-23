/**
 * Muse harness provider for the NanoClaw agent runner (muse accounts spec 3).
 * Installed by engines/install-engines.sh as container/agent-runner/src/providers/muse.ts.
 *
 * Drives `muse acp`, the CLI's Agent Client Protocol server over stdio. The worker
 * container and the OneCLI allow-list are the boundary, as for the Claude, Codex and
 * OpenCode harnesses: Muse's nested Bubblewrap sandbox cannot start under Docker's
 * default profiles, so it is off here and tool permission requests are allowed once.
 * The group directory is a trusted workspace: Muse then loads its composed CLAUDE.md and
 * starts the MCP servers given in session/new, neither of which it does otherwise.
 * The credential is the gateway sentinel in META_API_KEY; no real value enters the
 * container.
 */
import { type ChildProcessWithoutNullStreams, spawn } from "child_process";
import readline from "readline";

import { memoryContextForSessionStart } from "../memory/session-hook.js";
import { shimCwd } from "./cwd-shim.js";
import { registerProvider } from "./provider-registry.js";
import type {
  AgentProvider,
  AgentQuery,
  McpServerConfig,
  ProviderEvent,
  ProviderOptions,
  QueryInput,
} from "./types.js";

/** The error `session/load` returns for a session this host does not hold (Muse 1.4.25). */
const SESSION_NOT_FOUND = -32020;
const METHOD_NOT_FOUND = -32601;
const STDERR_TAIL_CHARS = 1000;
/** Session state, on the per-group mount of the host adapter (overlays/muse-host.ts). */
const MUSE_DATA_DIR = "/home/node/.muse-data";

type Json = Record<string, unknown>;

export interface MuseRuntime {
  command: string;
  commandArgs: string[];
  /** Holds the shared memory tree. */
  agentDir: string;
  /** XDG_DATA_HOME of the CLI: where its sessions persist. */
  dataDir: string;
}

const DEFAULT_RUNTIME: MuseRuntime = {
  command: "muse",
  commandArgs: [],
  agentDir: "/workspace/agent",
  dataDir: MUSE_DATA_DIR,
};

export class MuseAcpError extends Error {
  readonly code?: number;

  constructor(message: string, code?: number) {
    super(message);
    this.name = "MuseAcpError";
    this.code = code;
  }
}

/** The ACP form of the runner's MCP servers; ACP stdio servers have no cwd field. */
export function acpMcpServers(servers: Record<string, McpServerConfig>): Json[] {
  const pairs = (values: Record<string, string> = {}) =>
    Object.entries(values).map(([name, value]) => ({ name, value }));
  return Object.entries(servers).map(([name, config]) => {
    if (config.type === "http") {
      return { type: "http", name, url: config.url, headers: pairs(config.headers) };
    }
    const server = shimCwd(config) as Extract<McpServerConfig, { command: string }>;
    return { name, command: server.command, args: server.args ?? [], env: pairs(server.env) };
  });
}

class AcpConnection {
  onUpdate: ((update: Json) => void) | null = null;
  failure: MuseAcpError | null = null;
  private readonly child: ChildProcessWithoutNullStreams;
  private readonly calls = new Map<
    number,
    { resolve: (result: Json) => void; reject: (err: Error) => void }
  >();
  private nextId = 0;
  private stderr = "";

  constructor(child: ChildProcessWithoutNullStreams, onClosed: () => void) {
    this.child = child;
    const fail = (message: string) => {
      this.failure ??= new MuseAcpError(message);
      for (const call of this.calls.values()) call.reject(this.failure);
      this.calls.clear();
      onClosed();
    };
    child.on("error", (err) => fail(`muse acp could not start: ${err.message}`));
    // `close`, not `exit`: everything the agent wrote has been read by then.
    child.on("close", (code, signal) => {
      const tail = this.stderr.trim();
      fail(`muse acp exited (${signal ?? code})${tail ? `: ${tail}` : ""}`);
    });
    child.stdin.on("error", () => {}); // a dead agent surfaces through `close`
    child.stderr.on("data", (data) => {
      this.stderr = `${this.stderr}${data}`.slice(-STDERR_TAIL_CHARS);
    });
    readline.createInterface({ input: child.stdout }).on("line", (line) => this.receive(line));
  }

  request(method: string, params: Json): Promise<Json> {
    if (this.failure) return Promise.reject(this.failure);
    const id = ++this.nextId;
    return new Promise((resolve, reject) => {
      this.calls.set(id, { resolve, reject });
      this.write({ id, method, params });
    });
  }

  notify(method: string, params: Json): void {
    this.write({ method, params });
  }

  close(): void {
    this.child.kill(); // SIGTERM: the CLI flushes its session log first
  }

  private write(message: Json): void {
    if (!this.failure)
      this.child.stdin.write(`${JSON.stringify({ jsonrpc: "2.0", ...message })}\n`);
  }

  private receive(line: string): void {
    let message: Json;
    try {
      message = JSON.parse(line) as Json;
    } catch {
      return; // stdout carries ACP only; ignore anything else rather than end the turn
    }
    const { id, method, params } = message;
    if (typeof method === "string") {
      if (id !== undefined) this.answer(id, method, (params ?? {}) as Json);
      else if (method === "session/update") {
        this.onUpdate?.(((params ?? {}) as Json).update as Json);
      }
      return;
    }
    const call = typeof id === "number" ? this.calls.get(id) : undefined;
    if (!call) return;
    this.calls.delete(id as number);
    const error = message.error as { code?: number; message?: string } | undefined;
    if (error) call.reject(new MuseAcpError(error.message ?? "muse acp error", error.code));
    else call.resolve((message.result ?? {}) as Json);
  }

  private answer(id: unknown, method: string, params: Json): void {
    if (method !== "session/request_permission") {
      this.write({
        id,
        error: { code: METHOD_NOT_FOUND, message: `unsupported client method: ${method}` },
      });
      return;
    }
    const options = (params.options ?? []) as Array<{ optionId: string; kind?: string }>;
    const allow =
      options.find((o) => o.kind === "allow_once") ??
      options.find((o) => o.kind === "allow_always");
    this.write({
      id,
      result: {
        outcome: allow
          ? { outcome: "selected", optionId: allow.optionId }
          : { outcome: "cancelled" },
      },
    });
  }
}

interface QueryState {
  pending: string[];
  ended: boolean;
  aborted: boolean;
  wake: (() => void) | null;
  connection: AcpConnection | null;
  sessionId: string | null;
  inTurn: boolean;
}

const kick = (state: QueryState): void => {
  state.wake?.();
  state.wake = null;
};

export class MuseProvider implements AgentProvider {
  readonly supportsNativeSlashCommands = false;
  private readonly options: ProviderOptions;
  private readonly runtime: MuseRuntime;

  constructor(options: ProviderOptions = {}, runtime: MuseRuntime = DEFAULT_RUNTIME) {
    this.options = options;
    this.runtime = runtime;
  }

  // Muse has no session-start hook over ACP: a new session gets memory in its context.
  registerMemorySessionHook(): void {}

  isSessionInvalid(err: unknown): boolean {
    return err instanceof MuseAcpError && err.code === SESSION_NOT_FOUND;
  }

  query(input: QueryInput): AgentQuery {
    const state: QueryState = {
      pending: [input.prompt],
      ended: false,
      aborted: false,
      wake: null,
      connection: null,
      sessionId: null,
      inTurn: false,
    };
    return {
      // ACP has no steering: a follow-up runs as the next turn.
      push: (message) => {
        state.pending.push(message);
        kick(state);
      },
      end: () => {
        state.ended = true;
        kick(state);
      },
      abort: () => {
        state.aborted = true;
        if (state.connection && state.sessionId && state.inTurn) {
          state.connection.notify("session/cancel", { sessionId: state.sessionId });
        }
        kick(state);
      },
      events: this.run(input, state),
    };
  }

  private async *run(input: QueryInput, state: QueryState): AsyncGenerator<ProviderEvent> {
    const { model, effort } = this.options;
    if (!model) {
      // Never the CLI's own default: a worker switching engines must not run on a guess.
      const err = new MuseAcpError("muse needs an explicit model");
      yield { type: "error", message: err.message, retryable: false };
      throw err;
    }
    const child = spawn(
      this.runtime.command,
      [
        ...this.runtime.commandArgs,
        "acp",
        "--model",
        model,
        "--disable-sandbox",
        "--trust-workspace",
        "--disable-request-user-input",
        ...(effort ? ["-c", `reasoning_effort=${JSON.stringify(effort)}`] : []),
      ],
      {
        cwd: input.cwd,
        env: { ...process.env, ...this.options.env, XDG_DATA_HOME: this.runtime.dataDir },
        stdio: "pipe",
      },
    );
    const connection = new AcpConnection(child, () => kick(state));
    state.connection = connection;
    try {
      await connection.request("initialize", {
        protocolVersion: 1,
        clientCapabilities: { fs: { readTextFile: false, writeTextFile: false }, terminal: false },
      });
      const session = await this.openSession(connection, input);
      state.sessionId = session.id;
      yield { type: "init", continuation: session.id };

      let context = session.fresh ? this.sessionContext(input) : null;
      while (true) {
        while (!state.pending.length && !state.ended && !state.aborted && !connection.failure) {
          await new Promise<void>((resolve) => {
            state.wake = resolve;
          });
        }
        if (state.aborted) return;
        if (connection.failure && !state.ended) throw connection.failure;
        const text = state.pending.shift();
        if (text === undefined) return;
        yield* this.turn(connection, state, session.id, context ? [context, text] : [text]);
        context = null;
      }
    } catch (err) {
      if (state.aborted) return;
      const message = err instanceof Error ? err.message : String(err);
      yield { type: "error", message, retryable: false };
      throw err;
    } finally {
      state.connection = null;
      state.sessionId = null;
      connection.close();
    }
  }

  /** Resume the stored session; one this host no longer holds becomes a new session. */
  private async openSession(
    connection: AcpConnection,
    input: QueryInput,
  ): Promise<{ id: string; fresh: boolean }> {
    const base = {
      cwd: input.cwd,
      mcpServers: acpMcpServers(this.options.mcpServers ?? {}),
    };
    if (input.continuation) {
      try {
        // The replayed history arrives while onUpdate is unset, so it is not output.
        await connection.request("session/load", { ...base, sessionId: input.continuation });
        return { id: input.continuation, fresh: false };
      } catch (err) {
        if (!this.isSessionInvalid(err)) throw err;
      }
    }
    const created = await connection.request("session/new", base);
    if (typeof created.sessionId !== "string") {
      throw new MuseAcpError("muse acp returned no session id");
    }
    return { id: created.sessionId, fresh: true };
  }

  /** What a new session needs beyond the CLAUDE.md that Muse loads itself. */
  private sessionContext(input: QueryInput): string | null {
    const parts = [
      input.systemContext?.instructions,
      memoryContextForSessionStart("startup", this.runtime.agentDir),
    ].filter((part): part is string => !!part?.trim());
    return parts.length ? `<system>\n${parts.join("\n\n")}\n</system>` : null;
  }

  private async *turn(
    connection: AcpConnection,
    state: QueryState,
    sessionId: string,
    texts: string[],
  ): AsyncGenerator<ProviderEvent> {
    const buffer: ProviderEvent[] = [];
    const outcome: { done: boolean; stopReason?: unknown; failure?: Error } = { done: false };
    // The result is the last assistant message, as for the other harnesses.
    let message = "";
    let messageId: unknown;
    let afterTool = false;

    connection.onUpdate = (update) => {
      buffer.push({ type: "activity" });
      if (update?.sessionUpdate === "tool_call") afterTool = true;
      if (update?.sessionUpdate === "agent_message_chunk") {
        const content = update.content as { type?: string; text?: string } | undefined;
        if (content?.type === "text" && content.text) {
          if (afterTool || update.messageId !== messageId) message = "";
          afterTool = false;
          messageId = update.messageId;
          message += content.text;
        }
      }
      kick(state);
    };
    state.inTurn = true;
    void connection
      .request("session/prompt", {
        sessionId,
        prompt: texts.map((text) => ({ type: "text", text })),
      })
      .then(
        (result) => {
          outcome.stopReason = result.stopReason;
        },
        (err: Error) => {
          outcome.failure = err;
        },
      )
      .finally(() => {
        outcome.done = true;
        kick(state);
      });

    try {
      while (true) {
        while (buffer.length) yield buffer.shift() as ProviderEvent;
        if (outcome.done || state.aborted) break;
        await new Promise<void>((resolve) => {
          state.wake = resolve;
        });
      }
    } finally {
      connection.onUpdate = null;
      state.inTurn = false;
    }
    if (state.aborted) return;
    if (outcome.failure) throw outcome.failure;
    if (outcome.stopReason !== "end_turn") {
      throw new MuseAcpError(`Muse stopped the turn: ${String(outcome.stopReason)}`);
    }
    yield { type: "result", text: message || null };
  }
}

registerProvider("muse", (options) => new MuseProvider(options));
