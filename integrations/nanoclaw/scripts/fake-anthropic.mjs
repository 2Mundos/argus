#!/usr/bin/env node
// A local stand-in for the Anthropic Messages API (quota resume spike, no credentials).
// Appends every request body to $FAKE_ANTHROPIC_LOG (one JSON line each) and answers
// with fixed text, streamed as SSE when the request asks for it. Prints its port.
import { appendFileSync } from "node:fs";
import { createServer } from "node:http";

const log = process.env.FAKE_ANTHROPIC_LOG;
if (!log) throw new Error("FAKE_ANTHROPIC_LOG is required");
const REPLY = "ACK from the fake API.";

function sse(res, model) {
  res.writeHead(200, { "content-type": "text/event-stream", "cache-control": "no-cache" });
  const send = (event, data) => res.write(`event: ${event}\ndata: ${JSON.stringify(data)}\n\n`);
  const usage = { input_tokens: 10, output_tokens: 5 };
  send("message_start", {
    type: "message_start",
    message: {
      id: "msg_fake",
      type: "message",
      role: "assistant",
      model,
      content: [],
      stop_reason: null,
      stop_sequence: null,
      usage,
    },
  });
  send("content_block_start", {
    type: "content_block_start",
    index: 0,
    content_block: { type: "text", text: "" },
  });
  send("content_block_delta", {
    type: "content_block_delta",
    index: 0,
    delta: { type: "text_delta", text: REPLY },
  });
  send("content_block_stop", { type: "content_block_stop", index: 0 });
  send("message_delta", {
    type: "message_delta",
    delta: { stop_reason: "end_turn", stop_sequence: null },
    usage: { output_tokens: 5 },
  });
  send("message_stop", { type: "message_stop" });
  res.end();
}

const server = createServer((req, res) => {
  let body = "";
  req.on("data", (chunk) => {
    body += chunk;
  });
  req.on("end", () => {
    const url = new URL(req.url ?? "/", "http://fake");
    if (req.method !== "POST" || !url.pathname.startsWith("/v1/messages")) {
      res.writeHead(404, { "content-type": "application/json" });
      res.end(
        JSON.stringify({ type: "error", error: { type: "not_found_error", message: "fake" } }),
      );
      return;
    }
    if (url.pathname.endsWith("/count_tokens")) {
      res.writeHead(200, { "content-type": "application/json" });
      res.end(JSON.stringify({ input_tokens: 10 }));
      return;
    }
    let parsed = {};
    try {
      parsed = JSON.parse(body);
    } catch {}
    appendFileSync(log, `${JSON.stringify(parsed)}\n`);
    const model = parsed.model ?? "claude-fake";
    if (parsed.stream) return sse(res, model);
    res.writeHead(200, { "content-type": "application/json" });
    res.end(
      JSON.stringify({
        id: "msg_fake",
        type: "message",
        role: "assistant",
        model,
        content: [{ type: "text", text: REPLY }],
        stop_reason: "end_turn",
        stop_sequence: null,
        usage: { input_tokens: 10, output_tokens: 5 },
      }),
    );
  });
});

server.listen(0, "127.0.0.1", () => {
  process.stdout.write(`${server.address().port}\n`);
});
