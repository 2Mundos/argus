import {
  type ButtonHTMLAttributes,
  type MouseEvent,
  type ReactNode,
  useEffect,
  useRef,
  useState,
} from "react";

/** Existing CSS ring spinner; sized by the parent. */
export function Spinner({ className }: { className?: string }) {
  return <span className={["spinner", className].filter(Boolean).join(" ")} aria-hidden="true" />;
}

type PendingKey = string | true;

/** Shared pending state for forms and multi-button toolbars. */
export function useAction() {
  const [pending, setPending] = useState<PendingKey | null>(null);
  const pendingRef = useRef<PendingKey | null>(null);
  const mounted = useRef(true);

  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
    };
  }, []);

  async function run(fn: () => void | Promise<void>, key: PendingKey = true): Promise<void> {
    if (pendingRef.current !== null) return;
    pendingRef.current = key;
    setPending(key);
    try {
      await fn();
    } finally {
      pendingRef.current = null;
      if (mounted.current) setPending(null);
    }
  }

  return {
    busy: pending !== null,
    pending,
    is: (key: string) => pending === key,
    run,
  };
}

type ActionButtonProps = Omit<ButtonHTMLAttributes<HTMLButtonElement>, "onClick"> & {
  /** Uncontrolled: owns busy while this runs. */
  action?: () => void | Promise<void>;
  /** Controlled pending (e.g. from useAction). */
  pending?: boolean;
  /** Replaces children while pending; default keeps children. */
  pendingLabel?: ReactNode;
  onClick?: ButtonHTMLAttributes<HTMLButtonElement>["onClick"];
};

/**
 * Button that shows an in-button spinner and disables while a server action runs.
 * Use `action` alone for one-shot clicks, or `pending` from `useAction` in toolbars/forms.
 */
export function ActionButton({
  action,
  pending: pendingProp,
  pendingLabel,
  children,
  className,
  disabled,
  type = "button",
  onClick,
  ...rest
}: ActionButtonProps) {
  const [ownBusy, setOwnBusy] = useState(false);
  const ownBusyRef = useRef(false);
  const mounted = useRef(true);

  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
    };
  }, []);

  const pending = pendingProp === true || ownBusy;
  const label = pending && pendingLabel !== undefined ? pendingLabel : children;

  async function handleClick(e: MouseEvent<HTMLButtonElement>) {
    onClick?.(e);
    if (e.defaultPrevented || !action) return;
    if (pending || ownBusyRef.current) return;
    // Controlled `pending` means the parent (usually useAction) owns busy.
    if (pendingProp !== undefined) {
      await action();
      return;
    }
    ownBusyRef.current = true;
    setOwnBusy(true);
    try {
      await action();
    } finally {
      ownBusyRef.current = false;
      if (mounted.current) setOwnBusy(false);
    }
  }

  return (
    <button
      {...rest}
      type={type}
      className={[className, pending ? "is-pending" : ""].filter(Boolean).join(" ") || undefined}
      disabled={disabled || pending}
      aria-busy={pending || undefined}
      onClick={action || onClick ? handleClick : onClick}
    >
      {pending ? (
        <span className="btn-pending">
          <span className="btn-pending-sizer" aria-hidden="true">
            <Spinner />
            <span>{children}</span>
          </span>
          {pendingLabel !== undefined ? (
            <span className="btn-pending-sizer" aria-hidden="true">
              <Spinner />
              <span>{pendingLabel}</span>
            </span>
          ) : null}
          <span className="btn-pending-face">
            <Spinner />
            <span className="btn-pending-label">{label}</span>
          </span>
        </span>
      ) : (
        children
      )}
    </button>
  );
}
