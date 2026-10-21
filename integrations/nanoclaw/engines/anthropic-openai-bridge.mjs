/**
 * Loopback Anthropic Messages API → OpenAI Chat Completions for custom harness
 * (docs/specs/custom-harness.spec.md). The upstream URL and key are fixed at
 * bridge start; requests cannot retarget another host.
 */
import http from "node:http";
import { URL } from "node:url";

const MODEL_ID = /^[A-Za-z0-9][A-Za-z0-9._:/-]{0,199}$/;

function isObject(x) {
  return x !== null && typeof x === "object" && !Array.isArray(x);
}

/** True when `url` matches the configured upstream base (host + /v1 prefix). */
export function upstreamUrlAllowed(configuredBase, targetUrl) {
  try {
    const base = new URL(configuredBase.endsWith("/") ? configuredBase : `${configuredBase}/`);
    const target = new URL(targetUrl);
    if (base.protocol !== target.protocol || base.host !== target.host) return false;
    const basePath = base.pathname.replace(/\/+$/, "") || "";
    const targetPath = target.pathname.replace(/\/+$/, "") || "";
    return targetPath === `${basePath}/chat/completions` || targetPath.endsWith("/chat/completions");
  } catch {
    return false;
  }
}

function anthropicContentToOpenAI(content) {
  if (typeof content === "string") return content;
  if (!Array.isArray(content)) return "";
  const parts = [];
  const toolCalls = [];
  for (const block of content) {
    if (!isObject(block)) continue;
    if (block.type === "text" && typeof block.text === "string") parts.push(block.text);
    if (block.type === "tool_use" && typeof block.id === "string" && typeof block.name === "string") {
      toolCalls.push({
        id: block.id,
        type: "function",
        function: {
          name: block.name,
          arguments: JSON.stringify(block.input ?? {}),
        },
      });
    }
    if (block.type === "tool_result" && typeof block.tool_use_id === "string") {
      const text =
        typeof block.content === "string"
          ? block.content
          : Array.isArray(block.content)
            ? block.content
                .filter((c) => isObject(c) && c.type === "text")
                .map((c) => c.text)
                .join("")
            : "";
      return { role: "tool", tool_call_id: block.tool_use_id, content: text };
    }
  }
  if (toolCalls.length) return { content: parts.join("") || null, tool_calls: toolCalls };
  return parts.join("");
}

export function anthropicToOpenAIMessages(body) {
  const messages = [];
  if (typeof body.system === "string" && body.system.trim())
    messages.push({ role: "system", content: body.system });
  else if (Array.isArray(body.system))
    messages.push({
      role: "system",
      content: body.system
        .filter((b) => isObject(b) && b.type === "text")
        .map((b) => b.text)
        .join("\n"),
    });
  for (const msg of body.messages ?? []) {
    if (!isObject(msg) || typeof msg.role !== "string") continue;
    const converted = anthropicContentToOpenAI(msg.content);
    if (isObject(converted) && converted.role === "tool") {
      messages.push(converted);
      continue;
    }
    const out = { role: msg.role === "assistant" ? "assistant" : "user", content: converted };
    if (isObject(converted) && converted.tool_calls) {
      out.content = converted.content;
      out.tool_calls = converted.tool_calls;
    }
    messages.push(out);
  }
  return messages;
}

function anthropicToolsToOpenAI(tools) {
  if (!Array.isArray(tools)) return undefined;
  const out = [];
  for (const t of tools) {
    if (!isObject(t) || t.name == null) continue;
    out.push({
      type: "function",
      function: {
        name: String(t.name),
        description: typeof t.description === "string" ? t.description : undefined,
        parameters: isObject(t.input_schema) ? t.input_schema : { type: "object", properties: {} },
      },
    });
  }
  return out.length ? out : undefined;
}

export function buildOpenAIChatBody(anthropicBody, settings) {
  const model =
    settings.model ??
    (typeof anthropicBody.model === "string" && MODEL_ID.test(anthropicBody.model)
      ? anthropicBody.model
      : null);
  if (!model) throw new Error("no model configured for custom harness");
  const body = {
    model,
    messages: anthropicToOpenAIMessages(anthropicBody),
    stream: Boolean(anthropicBody.stream),
  };
  if (anthropicBody.max_tokens != null) body.max_tokens = anthropicBody.max_tokens;
  const tools = anthropicToolsToOpenAI(anthropicBody.tools);
  if (tools) body.tools = tools;
  if (settings.provider) body.provider = settings.provider;
  return body;
}

export function openAIChoiceToAnthropic(choice) {
  const message = choice?.message ?? {};
  const content = [];
  if (typeof message.content === "string" && message.content)
    content.push({ type: "text", text: message.content });
  for (const call of message.tool_calls ?? []) {
    if (!isObject(call) || call.type !== "function") continue;
    let input = {};
    try {
      input = JSON.parse(call.function?.arguments ?? "{}");
    } catch {
      input = {};
    }
    content.push({
      type: "tool_use",
      id: call.id ?? `tool_${content.length}`,
      name: call.function?.name ?? "tool",
      input,
    });
  }
  const stop = choice?.finish_reason === "tool_calls" ? "tool_use" : "end_turn";
  return {
    id: `msg_${randomId()}`,
    type: "message",
    role: "assistant",
    content,
    model: choice?.model ?? "",
    stop_reason: stop,
    stop_sequence: null,
    usage: {
      input_tokens: choice?.usage?.prompt_tokens ?? 0,
      output_tokens: choice?.usage?.completion_tokens ?? 0,
    },
  };
}

function randomId() {
  return Math.random().toString(36).slice(2, 14);
}

const sse = (res, type, data) => {
  res.write(`event: ${type}\ndata: ${JSON.stringify(data)}\n\n`);
};

/** Map an OpenAI chat-completions SSE body to Anthropic Messages SSE (text + tool_use). */
export async function pipeOpenAIChatStreamToAnthropic(res, reader) {
  const decoder = new TextDecoder();
  let buffer = "";
  const msgId = `msg_${randomId()}`;
  res.writeHead(200, {
    "content-type": "text/event-stream",
    "cache-control": "no-cache",
    connection: "keep-alive",
  });
  sse(res, "message_start", {
    type: "message_start",
    message: { id: msgId, type: "message", role: "assistant", content: [] },
  });

  let textBlockIndex = null;
  let nextBlockIndex = 0;
  const closedBlocks = new Set();
  /** @type {Map<number, { blockIndex: number, id: string, name: string, started: boolean }>} */
  const tools = new Map();
  let stopReason = "end_turn";

  const closeTextBlock = () => {
    if (textBlockIndex === null || closedBlocks.has(textBlockIndex)) return;
    sse(res, "content_block_stop", { type: "content_block_stop", index: textBlockIndex });
    closedBlocks.add(textBlockIndex);
    textBlockIndex = null;
  };

  const startTextBlock = () => {
    if (textBlockIndex !== null) return;
    textBlockIndex = nextBlockIndex++;
    sse(res, "content_block_start", {
      type: "content_block_start",
      index: textBlockIndex,
      content_block: { type: "text", text: "" },
    });
  };

  const startToolBlock = (toolIndex, id, name) => {
    closeTextBlock();
    const existing = tools.get(toolIndex);
    if (existing?.started) return existing;
    const blockIndex = nextBlockIndex++;
    const state = {
      blockIndex,
      id: id ?? `tool_${toolIndex}`,
      name: name ?? "tool",
      started: true,
    };
    tools.set(toolIndex, state);
    sse(res, "content_block_start", {
      type: "content_block_start",
      index: blockIndex,
      content_block: { type: "tool_use", id: state.id, name: state.name, input: {} },
    });
    return state;
  };

  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    const lines = buffer.split("\n");
    buffer = lines.pop() ?? "";
    for (const line of lines) {
      if (!line.startsWith("data: ")) continue;
      const payload = line.slice(6).trim();
      if (payload === "[DONE]") continue;
      try {
        const chunk = JSON.parse(payload);
        const choice = chunk.choices?.[0];
        if (!choice) continue;
        const delta = choice.delta ?? {};
        if (typeof delta.content === "string" && delta.content) {
          startTextBlock();
          sse(res, "content_block_delta", {
            type: "content_block_delta",
            index: textBlockIndex,
            delta: { type: "text_delta", text: delta.content },
          });
        }
        if (Array.isArray(delta.tool_calls)) {
          for (const tc of delta.tool_calls) {
            const ti = tc.index ?? 0;
            let state = tools.get(ti);
            if (!state?.started) state = startToolBlock(ti, tc.id, tc.function?.name);
            if (tc.id) state.id = tc.id;
            if (tc.function?.name) state.name = tc.function.name;
            const frag = tc.function?.arguments;
            if (typeof frag === "string" && frag) {
              sse(res, "content_block_delta", {
                type: "content_block_delta",
                index: state.blockIndex,
                delta: { type: "input_json_delta", partial_json: frag },
              });
            }
          }
        }
        if (choice.finish_reason === "tool_calls") stopReason = "tool_use";
        else if (choice.finish_reason === "length") stopReason = "max_tokens";
      } catch {
        /* ignore partial JSON lines */
      }
    }
  }

  closeTextBlock();
  for (const state of tools.values()) {
    if (!closedBlocks.has(state.blockIndex)) {
      sse(res, "content_block_stop", { type: "content_block_stop", index: state.blockIndex });
      closedBlocks.add(state.blockIndex);
    }
  }
  sse(res, "message_delta", {
    type: "message_delta",
    delta: { stop_reason: stopReason, stop_sequence: null },
  });
  sse(res, "message_stop", { type: "message_stop" });
  res.end();
}

export function quotaNoticeFromStatus(status, headers = {}) {
  if (status === 402) return "402 Payment required";
  if (status === 429) {
    const retry = headers["retry-after"] ?? headers["Retry-After"];
    if (retry) return `429 Rate limit (resets ${retry})`;
    return "429 Rate limit";
  }
  if (status === 401) return "401 Unauthorized";
  return null;
}

async function readJson(req) {
  const chunks = [];
  for await (const c of req) chunks.push(c);
  const raw = Buffer.concat(chunks).toString("utf8");
  if (!raw.trim()) return {};
  return JSON.parse(raw);
}

function writeAnthropicError(res, status, notice) {
  res.writeHead(status, { "content-type": "application/json" });
  res.end(
    JSON.stringify({
      type: "error",
      error: { type: "api_error", message: notice },
    }),
  );
}

/** Start a loopback bridge; `settings` has base_url, model, provider (no api_key). */
export async function startAnthropicOpenAIBridge({
  settings,
  apiKey,
  listenHost = "127.0.0.1",
  fetchImpl = fetch,
}) {
  if (!apiKey || typeof apiKey !== "string") throw new Error("bridge needs an api_key");
  const upstream = settings.base_url.replace(/\/+$/, "");
  const chatUrl = `${upstream}/chat/completions`;

  const server = http.createServer(async (req, res) => {
    if (req.method !== "POST" || !req.url?.startsWith("/v1/messages")) {
      res.writeHead(404);
      res.end();
      return;
    }
    let anthropicBody;
    try {
      anthropicBody = await readJson(req);
    } catch {
      writeAnthropicError(res, 400, "invalid request body");
      return;
    }
    let openBody;
    try {
      openBody = buildOpenAIChatBody(anthropicBody, settings);
    } catch (err) {
      writeAnthropicError(res, 400, String(err.message ?? err));
      return;
    }
    if (!upstreamUrlAllowed(upstream, chatUrl)) {
      writeAnthropicError(res, 500, "upstream URL not allowed");
      return;
    }
    let upstreamRes;
    try {
      upstreamRes = await fetchImpl(chatUrl, {
        method: "POST",
        headers: {
          "content-type": "application/json",
          authorization: `Bearer ${apiKey}`,
        },
        body: JSON.stringify(openBody),
      });
    } catch {
      writeAnthropicError(res, 502, "upstream unreachable");
      return;
    }
    const notice = quotaNoticeFromStatus(upstreamRes.status, upstreamRes.headers);
    if (notice) {
      writeAnthropicError(res, upstreamRes.status === 401 ? 401 : 429, notice);
      return;
    }
    if (!upstreamRes.ok) {
      writeAnthropicError(res, 502, `upstream HTTP ${upstreamRes.status}`);
      return;
    }
    if (openBody.stream) {
      const reader = upstreamRes.body?.getReader();
      if (!reader) {
        res.writeHead(502);
        res.end();
        return;
      }
      await pipeOpenAIChatStreamToAnthropic(res, reader);
      return;
    }
    const data = await upstreamRes.json();
    const choice = data.choices?.[0] ?? {};
    choice.model = data.model;
    choice.usage = data.usage;
    const anthropic = openAIChoiceToAnthropic(choice);
    anthropic.model = data.model ?? anthropic.model;
    res.writeHead(200, { "content-type": "application/json" });
    res.end(JSON.stringify(anthropic));
  });

  await new Promise((resolve, reject) => {
    server.once("error", reject);
    server.listen(0, listenHost, resolve);
  });
  const addr = server.address();
  const port = typeof addr === "object" && addr ? addr.port : 0;
  return {
    port,
    baseUrl: `http://${listenHost}:${port}`,
    async close() {
      await new Promise((resolve) => server.close(resolve));
    },
  };
}
