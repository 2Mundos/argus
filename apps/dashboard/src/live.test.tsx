// @vitest-environment jsdom
import { act, cleanup, render, screen } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { LiveProvider, reportPage, useLiveData } from "./live";

interface Stream {
  onStatus: (status: "connecting" | "live" | "offline") => void;
  onMessage: (data: string) => void;
}

const stream = vi.hoisted(() => ({ current: null as Stream | null }));

vi.mock("./sse", () => ({
  followEvents: (opts: Stream) => {
    stream.current = opts;
    opts.onStatus("live");
    return Promise.resolve();
  },
}));

const token = () => "tok";
const onUnauthorized = () => {};

function Host({ children }: { children: ReactNode }) {
  return (
    <LiveProvider token={token} onUnauthorized={onUnauthorized}>
      {children}
    </LiveProvider>
  );
}

afterEach(() => {
  cleanup();
  stream.current = null;
  vi.useRealTimers();
});

describe("useLiveData", () => {
  it("loads once when the stream first goes live", async () => {
    const load = vi.fn(async () => "ok");
    function Probe() {
      const { data } = useLiveData(load, []);
      return <span data-testid="data">{data ?? ""}</span>;
    }
    render(
      <Host>
        <Probe />
      </Host>,
    );
    expect(await screen.findByText("ok")).toBeTruthy();
    expect(load).toHaveBeenCalledTimes(1);
  });

  it("paints a partial result before the load finishes", async () => {
    let finish: (value: string) => void = () => {};
    function Probe() {
      const { data } = useLiveData((signal) => {
        reportPage(signal, "page-1");
        return new Promise<string>((resolve) => {
          finish = resolve;
        });
      }, []);
      return <span data-testid="data">{data ?? ""}</span>;
    }
    render(
      <Host>
        <Probe />
      </Host>,
    );
    expect(await screen.findByText("page-1")).toBeTruthy();
    await act(async () => {
      finish("all");
    });
    expect(screen.getByTestId("data").textContent).toBe("all");
  });

  it("keeps the first response when a refetch starts before anything is shown", async () => {
    const resolvers: ((value: string) => void)[] = [];
    const signals: AbortSignal[] = [];
    function Probe() {
      const { data } = useLiveData((signal) => {
        signals.push(signal);
        return new Promise<string>((resolve) => {
          resolvers.push(resolve);
        });
      }, []);
      return <span data-testid="data">{data ?? ""}</span>;
    }
    render(
      <Host>
        <Probe />
      </Host>,
    );
    await act(async () => {});
    expect(signals).toHaveLength(1);
    await act(async () => {
      stream.current?.onStatus("offline");
      stream.current?.onStatus("live");
    });
    expect(signals).toHaveLength(2);
    expect(signals[0]?.aborted).toBe(false);
    const first = resolvers[0];
    const second = resolvers[1];
    if (!first || !second) throw new Error("missing resolver");
    await act(async () => {
      first("first");
    });
    expect(screen.getByTestId("data").textContent).toBe("first");
    await act(async () => {
      second("second");
    });
    expect(screen.getByTestId("data").textContent).toBe("second");
  });

  it("keeps the current result while a refetch runs and aborts the one it replaces", async () => {
    vi.useFakeTimers();
    const resolvers: ((value: string) => void)[] = [];
    const signals: AbortSignal[] = [];
    function Probe() {
      const { data } = useLiveData((signal) => {
        signals.push(signal);
        return new Promise<string>((resolve, reject) => {
          signal.addEventListener("abort", () =>
            reject(new DOMException("The operation was aborted.", "AbortError")),
          );
          resolvers.push(resolve);
        });
      }, []);
      return <span data-testid="data">{data ?? ""}</span>;
    }
    render(
      <Host>
        <Probe />
      </Host>,
    );
    await act(async () => {});
    const first = resolvers[0];
    if (!first) throw new Error("missing resolver");
    await act(async () => {
      first("one");
    });
    expect(screen.getByTestId("data").textContent).toBe("one");
    await act(async () => {
      stream.current?.onMessage(JSON.stringify({ kind: "task.log" }));
      await vi.advanceTimersByTimeAsync(300);
    });
    expect(signals).toHaveLength(1);
    await act(async () => {
      stream.current?.onMessage(JSON.stringify({ kind: "task.updated" }));
      await vi.advanceTimersByTimeAsync(250);
    });
    expect(signals[0]?.aborted).toBe(true);
    expect(signals).toHaveLength(2);
    expect(screen.getByTestId("data").textContent).toBe("one");
    const second = resolvers[1];
    if (!second) throw new Error("missing resolver");
    await act(async () => {
      second("two");
    });
    expect(screen.getByTestId("data").textContent).toBe("two");
  });

  it("drops the previous result when the query changes", async () => {
    function List({ archived }: { archived: boolean }) {
      const { data } = useLiveData(
        (signal) => {
          if (!archived) return Promise.resolve("live");
          return new Promise<string>((_resolve, reject) => {
            signal.addEventListener("abort", () =>
              reject(new DOMException("The operation was aborted.", "AbortError")),
            );
          });
        },
        [archived],
      );
      return <span data-testid="data">{data ?? "empty"}</span>;
    }
    const view = render(
      <Host>
        <List archived={false} />
      </Host>,
    );
    expect(await screen.findByText("live")).toBeTruthy();
    view.rerender(
      <Host>
        <List archived />
      </Host>,
    );
    expect(await screen.findByText("empty")).toBeTruthy();
  });

  it("aborts an empty view's request on unmount", async () => {
    let signal: AbortSignal | undefined;
    function Probe() {
      const { data } = useLiveData((s) => {
        signal = s;
        return new Promise<string>(() => {});
      }, []);
      return <span>{data ?? ""}</span>;
    }
    const view = render(
      <Host>
        <Probe />
      </Host>,
    );
    await act(async () => {});
    view.unmount();
    expect(signal?.aborted).toBe(true);
  });

  it("ignores a partial from a request a newer one replaced", async () => {
    const signals: AbortSignal[] = [];
    const resolvers: ((value: string) => void)[] = [];
    function Probe() {
      const { data } = useLiveData((signal) => {
        signals.push(signal);
        return new Promise<string>((resolve) => resolvers.push(resolve));
      }, []);
      return <span data-testid="data">{data ?? ""}</span>;
    }
    render(
      <Host>
        <Probe />
      </Host>,
    );
    await act(async () => {});
    await act(async () => {
      stream.current?.onStatus("offline");
      stream.current?.onStatus("live");
    });
    const [first, second] = signals;
    if (!first || !second) throw new Error("missing signal");
    await act(async () => {
      reportPage(first, "stale-partial");
    });
    expect(screen.getByTestId("data").textContent).toBe("");
  });

  it("drops shown data and ignores the old response when deps change mid-flight", async () => {
    const resolvers = new Map<string, (value: string) => void>();
    function List({ q }: { q: string }) {
      const { data } = useLiveData(
        () => new Promise<string>((resolve) => resolvers.set(q, resolve)),
        [q],
      );
      return <span data-testid="data">{data ?? "empty"}</span>;
    }
    const view = render(
      <Host>
        <List q="a" />
      </Host>,
    );
    await act(async () => {});
    await act(async () => {
      resolvers.get("a")?.("a-result");
    });
    expect(screen.getByTestId("data").textContent).toBe("a-result");
    view.rerender(
      <Host>
        <List q="b" />
      </Host>,
    );
    expect(screen.getByTestId("data").textContent).toBe("empty");
    await act(async () => {
      resolvers.get("b")?.("b-result");
    });
    expect(screen.getByTestId("data").textContent).toBe("b-result");
  });
});
