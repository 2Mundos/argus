import { type FormEvent, useEffect, useRef, useState } from "react";
import { ActionButton, useAction } from "../ActionButton";
import { api, call, setToken } from "../api";
import { Brand } from "../Brand";

/** Matched by `.login form input:-webkit-autofill` in styles.css. */
export const LOGIN_AUTOFILL_ANIMATION = "login-autofill-start";

export function Login({ onLogin }: { onLogin: () => void }) {
  const inputRef = useRef<HTMLInputElement>(null);
  const [value, setValue] = useState("");
  const [error, setError] = useState<string>();
  const action = useAction();

  function syncFromInput() {
    const next = inputRef.current?.value ?? "";
    setValue((prev) => (prev === next ? prev : next));
  }

  // Password managers can fill the field without a React change event.
  useEffect(() => {
    syncFromInput();
    const raf = requestAnimationFrame(syncFromInput);
    const soon = window.setTimeout(syncFromInput, 100);
    const later = window.setTimeout(syncFromInput, 500);
    return () => {
      cancelAnimationFrame(raf);
      clearTimeout(soon);
      clearTimeout(later);
    };
  }, []);

  async function submit(e: FormEvent) {
    e.preventDefault();
    const trimmed = (inputRef.current?.value ?? value).trim();
    await action.run(async () => {
      setError(undefined);
      setToken(trimmed);
      try {
        await call(api.GET("/api/agents"));
        onLogin();
      } catch {
        setToken(null);
        setError("That token was not accepted.");
      }
    });
  }

  return (
    <main className="login">
      <form onSubmit={submit} className="card">
        <Brand as="h1" />
        <p className="muted">
          Paste an admin token. The first one is in <code>/data/admin-token</code>.
        </p>
        <input
          ref={inputRef}
          type="password"
          autoComplete="current-password"
          aria-label="Token"
          value={value}
          onChange={(e) => setValue(e.target.value)}
          onInput={syncFromInput}
          onFocus={syncFromInput}
          onAnimationStart={(e) => {
            if (e.animationName === LOGIN_AUTOFILL_ANIMATION) syncFromInput();
          }}
          placeholder="argus_admin_…"
          required
        />
        {error && <p className="error">{error}</p>}
        <ActionButton
          type="submit"
          className="primary"
          disabled={!value.trim()}
          pending={action.busy}
          pendingLabel="Checking…"
        >
          Sign in
        </ActionButton>
      </form>
    </main>
  );
}
