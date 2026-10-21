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
  /** Bumped (debounced) whenever events arrive; use as a refetch dependency. */
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
  const { token, onUnauthorized } = props;

  useEffect(() => {
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout> | undefined;
    followEvents({
      token,
      onUnauthorized,
      onStatus: (s) => {
        setStatus(s);
        if (s === "live") {
          setVersion((v) => v + 1); // refetch after (re)connecting
          setConnection((c) => c + 1);
        }
      },
      onMessage: (data) => {
        const event = JSON.parse(data) as ArgusEvent;
        for (const listener of listeners.current) listener(event);
        if (event.kind === "task.log") return; // output only: no state to refetch
        clearTimeout(timer);
        timer = setTimeout(() => setVersion((v) => v + 1), 250);
      },
      signal: controller.signal,
    });
    return () => {
      controller.abort();
      clearTimeout(timer);
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

/** Load data and reload it whenever live events arrive (or deps change). */
export function useLiveData<T>(
  load: () => Promise<T>,
  deps: unknown[],
): { data: T | undefined; error: Error | undefined; reload: () => void } {
  const { version } = useLive();
  const [data, setData] = useState<T>();
  const [error, setError] = useState<Error>();
  const [nonce, setNonce] = useState(0);
  useEffect(() => {
    let cancelled = false;
    load().then(
      (value) => {
        if (!cancelled) {
          setData(value);
          setError(undefined);
        }
      },
      (err: Error) => {
        if (!cancelled) setError(err);
      },
    );
    return () => {
      cancelled = true;
    };
  }, [version, nonce, ...deps]);
  return { data, error, reload: () => setNonce((n) => n + 1) };
}
