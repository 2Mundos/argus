import {
  createContext,
  type ReactNode,
  useCallback,
  useContext,
  useEffect,
  useRef,
  useState,
} from "react";
import type { ArgusEvent } from "./api";
import { followEvents, type StreamStatus } from "./sse";

interface Live {
  status: StreamStatus;
  /** Bumped (debounced) when events arrive, and again each time the stream reconnects. */
  version: number;
  /** Bumped each time the stream (re)connects; transient events sent while it was down are gone. */
  connection: number;
  subscribe: (listener: (event: ArgusEvent) => void) => () => void;
}

const LiveContext = createContext<Live | null>(null);

export function LiveProvider(props: {
  token: () => string | null;
  onUnauthorized: () => void;
  children: ReactNode;
}) {
  const [status, setStatus] = useState<StreamStatus>("connecting");
  const [version, setVersion] = useState(0);
  const [connection, setConnection] = useState(0);
  const listeners = useRef(new Set<(event: ArgusEvent) => void>());
  const seenLive = useRef(false);
  const { token, onUnauthorized } = props;

  useEffect(() => {
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout> | undefined;
    followEvents({
      token,
      onUnauthorized,
      onStatus: (next) => {
        setStatus(next);
        if (next !== "live") return;
        // The mount fetch is already a fresh snapshot. Bump only on reconnect,
        // when events published while the stream was down are gone.
        if (seenLive.current) setVersion((v) => v + 1);
        seenLive.current = true;
        setConnection((c) => c + 1);
      },
      onMessage: (data) => {
        const event = JSON.parse(data) as ArgusEvent;
        for (const listener of listeners.current) listener(event);
        // output only: no state to refetch (TaskChanges listens for its own)
        if (event.kind === "task.log" || event.kind === "task.changes") return;
        clearTimeout(timer);
        timer = setTimeout(() => setVersion((v) => v + 1), 250);
      },
      signal: controller.signal,
    });
    return () => {
      controller.abort();
      clearTimeout(timer);
      // A restarted effect's first live is a new snapshot, not a reconnect.
      seenLive.current = false;
    };
  }, [token, onUnauthorized]);

  const subscribe = useCallback((listener: (event: ArgusEvent) => void) => {
    listeners.current.add(listener);
    return () => listeners.current.delete(listener);
  }, []);

  return (
    <LiveContext.Provider value={{ status, version, connection, subscribe }}>
      {props.children}
    </LiveContext.Provider>
  );
}

export function useLive(): Live {
  const live = useContext(LiveContext);
  if (!live) throw new Error("useLive outside LiveProvider");
  return live;
}

function sameDeps(a: unknown[], b: unknown[]): boolean {
  return a.length === b.length && a.every((value, i) => Object.is(value, b[i]));
}

function isAbort(err: unknown): boolean {
  return err instanceof Error && err.name === "AbortError";
}

const partials = new WeakMap<AbortSignal, (partial: unknown) => void>();

/** Show a partial result for the `useLiveData` load started with this signal. */
export function reportPage<T>(signal: AbortSignal, partial: T): void {
  partials.get(signal)?.(partial);
}

/**
 * Load data and reload it whenever live events arrive (or deps change).
 * `load` may call `reportPage` with the signal it was given (the Tasks board
 * paints page one this way). A later response replaces it. While a refetch of
 * the same query is in flight the previous result stays on screen.
 */
export function useLiveData<T>(
  load: (signal: AbortSignal) => Promise<T>,
  deps: unknown[],
): { data: T | undefined; error: Error | undefined; reload: () => void } {
  const { version } = useLive();
  const [data, setData] = useState<T>();
  const [error, setError] = useState<Error>();
  const [nonce, setNonce] = useState(0);
  const dataRef = useRef(data);
  dataRef.current = data;
  const depsRef = useRef<unknown[] | null>(null);
  const queryGen = useRef(0);
  const latest = useRef(0);
  const inflight = useRef(new Set<AbortController>());
  const mounted = useRef(true);

  // Declared before the load effect: cleanups run in order, so on unmount
  // `mounted` is already false when the load cleanup checks it.
  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
    };
  }, []);

  useEffect(() => {
    const changed = depsRef.current !== null && !sameDeps(depsRef.current, deps);
    depsRef.current = deps;
    if (changed) {
      queryGen.current += 1;
      if (dataRef.current !== undefined) {
        dataRef.current = undefined;
        setData(undefined);
        setError(undefined);
      }
    }
    const hadData = dataRef.current !== undefined;
    const query = queryGen.current;
    const id = ++latest.current;
    const controller = new AbortController();
    inflight.current.add(controller);
    let published = false;

    const paint = (value: T) => {
      if (!mounted.current || query !== queryGen.current) return;
      const stale = latest.current !== id;
      // A superseded response may still be the first thing on screen.
      if (stale && dataRef.current !== undefined) return;
      dataRef.current = value;
      setData(value);
      setError(undefined);
      if (!stale) {
        for (const other of [...inflight.current]) {
          if (other !== controller) other.abort();
        }
      }
    };

    partials.set(controller.signal, (partial) => {
      if (query !== queryGen.current || latest.current !== id || hadData) return;
      // Keep appending this request's pages, but don't let them replace a
      // result another request already showed.
      if (dataRef.current !== undefined && !published) return;
      published = true;
      paint(partial as T);
    });

    load(controller.signal)
      .then(
        (value) => {
          paint(value);
        },
        (err: unknown) => {
          if (!mounted.current || query !== queryGen.current || latest.current !== id) return;
          if (isAbort(err)) return;
          setError(err instanceof Error ? err : new Error(String(err)));
        },
      )
      .finally(() => {
        inflight.current.delete(controller);
      });

    return () => {
      partials.delete(controller.signal);
      // Unmount aborts even an empty view. A newer load of the same query does
      // not, so the response already in flight can still paint.
      if (!mounted.current || dataRef.current !== undefined) {
        controller.abort();
        inflight.current.delete(controller);
      }
    };
  }, [version, nonce, ...deps]);

  return { data, error, reload: () => setNonce((n) => n + 1) };
}
