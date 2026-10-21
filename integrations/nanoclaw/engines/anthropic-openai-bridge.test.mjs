import assert from "node:assert/strict";
import http from "node:http";
import { after, test } from "node:test";
import {
  anthropicToOpenAIMessages,
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
  assert.deepEqual(msgs, [{ role: "system", content: "sys" }, { role: "user", content: "hi" }]);
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
    seen.push({ auth: req.headers.authorization, body: JSON.parse(Buffer.concat(chunks).toString()) });
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
