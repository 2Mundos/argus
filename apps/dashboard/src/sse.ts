interface SseMessage {
  id: string | null;
  data: string;
}

/** Split a server-sent-events buffer into complete messages and the remainder. */
export function parseSse(buffer: string): { messages: SseMessage[]; rest: string } {
  const blocks = buffer.replace(/\r\n/g, "\n").split("\n\n");
  const rest = blocks.pop() ?? "";
  const messages: SseMessage[] = [];
  for (const block of blocks) {
    let id: string | null = null;
    const data: string[] = [];
    for (const line of block.split("\n")) {
      if (line.startsWith(":")) continue; // comment / keepalive
      const sep = line.indexOf(":");
      const field = sep === -1 ? line : line.slice(0, sep);
      const value = sep === -1 ? "" : line.slice(sep + 1).replace(/^ /, "");
      if (field === "id") id = value;
      else if (field === "data") data.push(value);
    }
    if (data.length > 0) messages.push({ id, data: data.join("\n") });
  }
  return { messages, rest };
}

export type StreamStatus = "connecting" | "live" | "offline";

/**
 * Follow /api/events/stream with fetch (EventSource cannot send an Authorization
 * header). Reconnects with Last-Event-ID so no event is missed.
 */
export async function followEvents(opts: {
  token: () => string | null;
  onMessage: (data: string) => void;
  onStatus: (status: StreamStatus) => void;
  onUnauthorized: () => void;
  signal: AbortSignal;
}): Promise<void> {
  let lastId: string | null = null;
  while (!opts.signal.aborted) {
    opts.onStatus("connecting");
    try {
      const headers: Record<string, string> = { Authorization: `Bearer ${opts.token()}` };
      if (lastId) headers["Last-Event-ID"] = lastId;
      const res = await fetch("/api/events/stream", { headers, signal: opts.signal });
      if (res.status === 401) {
        opts.onUnauthorized();
        return;
      }
      if (!res.ok || !res.body) throw new Error(`stream failed: ${res.status}`);
      opts.onStatus("live");
      const reader = res.body.pipeThrough(new TextDecoderStream()).getReader();
      let buffer = "";
      for (;;) {
        const { value, done } = await reader.read();
        if (done) break;
        const parsed = parseSse(buffer + value);
        buffer = parsed.rest;
        for (const message of parsed.messages) {
          if (message.id) lastId = message.id;
          opts.onMessage(message.data);
        }
      }
    } catch {
      if (opts.signal.aborted) return;
    }
    opts.onStatus("offline");
    await new Promise((resolve) => setTimeout(resolve, 2000));
  }
}
