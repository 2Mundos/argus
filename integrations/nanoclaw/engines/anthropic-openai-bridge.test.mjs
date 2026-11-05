import assert from "node:assert/strict";
import http from "node:http";
import { after, test } from "node:test";
import {
  anthropicToOpenAIMessages,
  BridgeRequestError,
  buildOpenAIChatBody,
  openAIChoiceToAnthropic,
  pipeOpenAIChatStreamToAnthropic,
  startAnthropicOpenAIBridge,
} from "./anthropic-openai-bridge.mjs";

test("anthropic messages become OpenAI chat messages", () => {
  const msgs = anthropicToOpenAIMessages({
    system: "sys",
    messages: [{ role: "user", content: "hi" }],
  });
  assert.deepEqual(msgs, [
    { role: "system", content: "sys" },
    { role: "user", content: "hi" },
  ]);
});

test("buildOpenAIChatBody rejects a missing model as a bridge error", () => {
  assert.throws(
    () => buildOpenAIChatBody({ messages: [] }, {}),
    (err) => err instanceof BridgeRequestError && /no model configured/.test(err.message),
  );
});

test("buildOpenAIChatBody adds model and provider block", () => {
  const body = buildOpenAIChatBody(
    { messages: [{ role: "user", content: "x" }] },
    {
      base_url: "https://openrouter.ai/api/v1",
      model: "deepseek/deepseek-v4.1-flash-1017",
      provider: { order: ["DeepInfra"] },
    },
  );
  assert.equal(body.model, "deepseek/deepseek-v4.1-flash-1017");
  assert.deepEqual(body.provider, { order: ["DeepInfra"] });
});

test("bridge forwards to upstream and returns anthropic shape", async (t) => {
  const seen = [];
  const upstream = http.createServer(async (req, res) => {
    if (!req.url?.endsWith("/chat/completions")) {
      res.writeHead(404);
      res.end();
      return;
    }
    const chunks = [];
    for await (const c of req) chunks.push(c);
    seen.push({
      auth: req.headers.authorization,
      body: JSON.parse(Buffer.concat(chunks).toString()),
    });
    res.writeHead(200, { "content-type": "application/json" });
    res.end(
      JSON.stringify({
        model: "deepseek/deepseek-v4.1-flash-1017",
        choices: [{ message: { role: "assistant", content: "ok" }, finish_reason: "stop" }],
        usage: { prompt_tokens: 1, completion_tokens: 2 },
      }),
    );
  });
  await new Promise((r) => upstream.listen(0, "127.0.0.1", r));
  t.after(() => upstream.close());
  const port = upstream.address().port;
  const bridge = await startAnthropicOpenAIBridge({
    settings: {
      base_url: `http://127.0.0.1:${port}/api/v1`,
      model: "deepseek/deepseek-v4.1-flash-1017",
      provider: null,
    },
    apiKey: "sk-test-key",
  });
  after(async () => bridge.close());
  const res = await fetch(`${bridge.baseUrl}/v1/messages`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({
      model: "ignored",
      max_tokens: 10,
      messages: [{ role: "user", content: "ping" }],
    }),
  });
  const raw = await res.text();
  assert.equal(res.status, 200, raw);
  const out = JSON.parse(raw);
  assert.equal(out.role, "assistant");
  assert.ok(seen.length > 0);
  assert.equal(seen[0].auth, "Bearer sk-test-key");
  assert.equal(seen[0].body.model, "deepseek/deepseek-v4.1-flash-1017");
  assert.ok(!JSON.stringify(seen[0].body).includes("sk-test-key"));
});

test("streaming forwards tool_call deltas and stop_reason tool_use", async () => {
  const chunks = [
    'data: {"choices":[{"index":0,"delta":{"tool_calls":[{"index":0,"id":"call_1","function":{"name":"bash","arguments":"{"}}]}}]}\n\n',
    'data: {"choices":[{"index":0,"delta":{"tool_calls":[{"index":0,"function":{"arguments":"\\"cmd\\":\\"ls\\"}"}}]}}]}\n\n',
    'data: {"choices":[{"index":0,"delta":{},"finish_reason":"tool_calls"}]}\n\n',
    "data: [DONE]\n\n",
  ];
  let i = 0;
  const reader = {
    async read() {
      if (i >= chunks.length) return { done: true };
      return { done: false, value: new TextEncoder().encode(chunks[i++]) };
    },
  };
  const events = [];
  const res = {
    writeHead() {},
    write(line) {
      events.push(line);
    },
    end() {},
  };
  await pipeOpenAIChatStreamToAnthropic(res, reader);
  const joined = events.join("");
  assert.match(joined, /"type":"tool_use"/);
  assert.match(joined, /input_json_delta/);
  assert.match(joined, /"stop_reason":"tool_use"/);
});

test("openAIChoiceToAnthropic maps tool calls", () => {
  const msg = openAIChoiceToAnthropic({
    message: {
      content: "",
      tool_calls: [
        {
          id: "t1",
          type: "function",
          function: { name: "bash", arguments: '{"cmd":"ls"}' },
        },
      ],
    },
    finish_reason: "tool_calls",
  });
  assert.equal(msg.stop_reason, "tool_use");
  assert.equal(msg.content[0].type, "tool_use");
});

test("google thought signatures survive an anthropic tool round trip", () => {
  const signature = "sig+/=";
  const msg = openAIChoiceToAnthropic({
    message: {
      content: "",
      tool_calls: [
        {
          id: "call_1",
          type: "function",
          extra_content: { google: { thought_signature: signature } },
          function: { name: "bash", arguments: '{"cmd":"ls"}' },
        },
      ],
    },
    finish_reason: "tool_calls",
  });
  const toolUse = msg.content[0];
  assert.equal(toolUse.type, "tool_use");
  assert.notEqual(toolUse.id, "call_1");

  const back = anthropicToOpenAIMessages({
    messages: [
      { role: "assistant", content: [toolUse] },
      {
        role: "user",
        content: [{ type: "tool_result", tool_use_id: toolUse.id, content: "ok" }],
      },
    ],
  });
  assert.equal(back[0].tool_calls[0].id, "call_1");
  assert.deepEqual(back[0].tool_calls[0].extra_content, {
    google: { thought_signature: signature },
  });
  assert.deepEqual(back[1], { role: "tool", tool_call_id: "call_1", content: "ok" });
});

test("streaming keeps a google thought signature and reports tool_use on stop", async () => {
  const chunks = [
    'data: {"choices":[{"index":0,"delta":{"tool_calls":[{"index":0,"id":"call_1","extra_content":{"google":{"thought_signature":"sig"}},"function":{"name":"bash","arguments":"{}"}}]}}]}\n\n',
    'data: {"choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}\n\n',
    "data: [DONE]\n\n",
  ];
  let i = 0;
  const reader = {
    async read() {
      if (i >= chunks.length) return { done: true };
      return { done: false, value: new TextEncoder().encode(chunks[i++]) };
    },
  };
  const events = [];
  const res = {
    writeHead() {},
    write(line) {
      events.push(line);
    },
    end() {},
  };
  await pipeOpenAIChatStreamToAnthropic(res, reader);
  const joined = events.join("");
  assert.match(joined, /"type":"tool_use"/);
  assert.match(joined, /__argus_thought__/);
  assert.match(joined, /"stop_reason":"tool_use"/);
});

test("bridge echoes its validation error and hides other failures", async () => {
  const open = await startAnthropicOpenAIBridge({
    settings: { base_url: "http://127.0.0.1:9/v1" },
    apiKey: "k",
  });
  try {
    const res = await fetch(`${open.baseUrl}/v1/messages`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: "{}",
    });
    const raw = await res.text();
    assert.equal(res.status, 400, raw);
    assert.equal(JSON.parse(raw).error.message, "no model configured for custom harness");
  } finally {
    await open.close();
  }

  const typed = await startAnthropicOpenAIBridge({
    settings: { base_url: "http://127.0.0.1:9/v1", model: "m" },
    apiKey: "k",
  });
  const logged = [];
  const orig = console.error;
  console.error = (...args) => {
    logged.push(args);
  };
  try {
    const res = await fetch(`${typed.baseUrl}/v1/messages`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: "null",
    });
    const raw = await res.text();
    assert.equal(res.status, 400, raw);
    assert.equal(JSON.parse(raw).error.message, "invalid request");
    assert.equal(raw.includes("Cannot read"), false);
    assert.equal(raw.includes("system"), false);
    assert.ok(logged.length > 0);
    assert.match(String(logged[0][1]), /Cannot read properties of null/);
  } finally {
    console.error = orig;
    await typed.close();
  }
});

test("a bridge beyond loopback requires a bridge key", async () => {
  const { startAnthropicOpenAIBridge } = await import("./anthropic-openai-bridge.mjs");
  await assert.rejects(
    startAnthropicOpenAIBridge({
      settings: { base_url: "http://127.0.0.1:9/v1" },
      apiKey: "k",
      listenHost: "0.0.0.0",
    }),
    /bridgeKey/,
  );
});

test("bridgeKeyMatches is exact", async () => {
  const { bridgeKeyMatches } = await import("./anthropic-openai-bridge.mjs");
  assert.equal(bridgeKeyMatches("abc", "abc"), true);
  assert.equal(bridgeKeyMatches("abd", "abc"), false);
  assert.equal(bridgeKeyMatches(undefined, "abc"), false);
  assert.equal(bridgeKeyMatches(["abc"], "abc"), false);
});
