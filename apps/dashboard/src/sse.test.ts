import { describe, expect, it } from "vitest";
import { parseSse } from "./sse";

describe("parseSse", () => {
  it("splits complete messages and keeps the remainder", () => {
    const { messages, rest } = parseSse('id: 1\ndata: {"a":1}\n\nid: 2\ndata: {"b"');
    expect(messages).toEqual([{ id: "1", data: '{"a":1}' }]);
    expect(rest).toBe('id: 2\ndata: {"b"');
  });

  it("ignores keepalive comments", () => {
    expect(parseSse(": keepalive\n\n").messages).toEqual([]);
  });

  it("joins multi-line data and handles CRLF", () => {
    const { messages } = parseSse("data: a\r\ndata: b\r\n\r\n");
    expect(messages).toEqual([{ id: null, data: "a\nb" }]);
  });
});
