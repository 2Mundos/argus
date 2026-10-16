import { type FormEvent, useCallback, useEffect, useRef, useState } from "react";
import { api, call } from "../api";
import { type AuthFileCheck, checkAuthFile, safeLoginUrl } from "../codexAuth";

type Login = {
  login_id: string;
  state: string;
  verification_url: string | null;
  user_code: string | null;
  expires_in_s: number;
  account_id: string | null;
  error: string | null;
};

const POLL_MS = 2000;

/** Add a Codex (ChatGPT) account: import an auth.json, or generate one by the
 * official dedicated login. Both end as the same encrypted account. */
export function CodexAccount({ onClose, onDone }: { onClose: () => void; onDone: () => void }) {
  const [path, setPath] = useState<"import" | "login">("import");
  return (
    <div className="overlay">
      <div className="dialog" role="dialog" aria-label="Add Codex account">
        <h2>Add Codex account</h2>
        <p className="muted small">
          Codex runs on a ChatGPT login. The login is stored encrypted as an Argus account and is
          never shown again; workers receive it through the vault, not as a file.
        </p>
        <div className="row" role="tablist">
          <button
            type="button"
            role="tab"
            aria-selected={path === "import"}
            className={path === "import" ? "primary" : ""}
            onClick={() => setPath("import")}
          >
            Import auth.json
          </button>
          <button
            type="button"
            role="tab"
            aria-selected={path === "login"}
            className={path === "login" ? "primary" : ""}
            onClick={() => setPath("login")}
          >
            Generate with a dedicated login
          </button>
        </div>
        {path === "import" ? (
          <ImportAuth onClose={onClose} onDone={onDone} />
        ) : (
          <DedicatedLogin onClose={onClose} onDone={onDone} />
        )}
      </div>
    </div>
  );
}

function ImportAuth({ onClose, onDone }: { onClose: () => void; onDone: () => void }) {
  const [name, setName] = useState("Codex (imported)");
  const [text, setText] = useState("");
  const [fileName, setFileName] = useState<string>();
  const [error, setError] = useState<string>();
  const [busy, setBusy] = useState(false);
  const check: AuthFileCheck | null = text.trim() ? checkAuthFile(text) : null;

  async function pick(file: File | undefined) {
    setError(undefined);
    if (!file) return;
    setFileName(file.name);
    setText(await file.text());
  }

  async function submit(e: FormEvent) {
    e.preventDefault();
    setError(undefined);
    const result = checkAuthFile(text);
    if (!result.ok) {
      setError(result.error);
      return;
    }
    setBusy(true);
    try {
      await call(
        api.POST("/api/accounts", {
          body: {
            name,
            provider: "openai",
            kind: result.kind,
            credential: JSON.parse(text),
          },
        }),
      );
      setText(""); // the document does not stay in the page
      onDone();
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <form onSubmit={submit}>
      <p className="muted small">
        Use a login you are already authorised to use, for example the Codex Desktop{" "}
        <code>auth.json</code>. Using the same login on the Desktop at the same time can invalidate
        it; prefer a dedicated login for accounts that run unattended.
      </p>
      <label>
        Name
        <input value={name} onChange={(e) => setName(e.target.value)} required maxLength={100} />
      </label>
      <label>
        auth.json file
        <input
          type="file"
          accept=".json,application/json"
          aria-label="auth.json file"
          onChange={(e) => pick(e.target.files?.[0])}
        />
      </label>
      <label>
        …or paste its content
        <textarea
          rows={4}
          aria-label="auth.json content"
          value={text}
          onChange={(e) => setText(e.target.value)}
          spellCheck={false}
          autoComplete="off"
          placeholder="{ … }"
        />
      </label>
      {check?.ok && (
        <p className="small" role="status">
          {fileName ? `${fileName}: ` : ""}
          {check.mode === "chatgpt" ? "ChatGPT login" : "API key"} detected (values hidden).
        </p>
      )}
      {check && !check.ok && <p className="error">{check.error}</p>}
      {error && <p className="error">{error}</p>}
      <div className="actions">
        <button type="button" onClick={onClose}>
          Cancel
        </button>
        <button type="submit" className="primary" disabled={busy || !check?.ok}>
          Import
        </button>
      </div>
    </form>
  );
}

function DedicatedLogin({ onClose, onDone }: { onClose: () => void; onDone: () => void }) {
  const [name, setName] = useState("Codex (login)");
  const [login, setLogin] = useState<Login>();
  const [error, setError] = useState<string>();
  const [busy, setBusy] = useState(false);
  const id = login?.state === "pending" ? login.login_id : undefined;
  const done = useRef(onDone);
  done.current = onDone;

  const poll = useCallback(async (loginId: string) => {
    try {
      const next = (await call(
        api.GET("/api/accounts/codex-login/{login_id}", {
          params: { path: { login_id: loginId } },
        }),
      )) as Login;
      setLogin(next);
      if (next.state === "completed") done.current();
    } catch (err) {
      setError((err as Error).message);
    }
  }, []);

  useEffect(() => {
    if (!id) return;
    const timer = setInterval(() => void poll(id), POLL_MS);
    return () => clearInterval(timer);
  }, [id, poll]);

  async function start(e: FormEvent) {
    e.preventDefault();
    setError(undefined);
    setBusy(true);
    try {
      setLogin((await call(api.POST("/api/accounts/codex-login", { body: { name } }))) as Login);
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setBusy(false);
    }
  }

  async function cancel() {
    if (!login) return onClose();
    try {
      if (login.state === "pending") {
        setLogin(
          (await call(
            api.DELETE("/api/accounts/codex-login/{login_id}", {
              params: { path: { login_id: login.login_id } },
            }),
          )) as Login,
        );
      }
    } catch (err) {
      setError((err as Error).message);
      return;
    }
    onClose();
  }

  if (!login) {
    return (
      <form onSubmit={start}>
        <p className="muted small">
          Argus runs the official Codex sign-in for this account only. You get a link and a one-time
          code to enter in your browser; nothing is read from this computer.
        </p>
        <label>
          Name
          <input value={name} onChange={(e) => setName(e.target.value)} required maxLength={100} />
        </label>
        {error && <p className="error">{error}</p>}
        <div className="actions">
          <button type="button" onClick={onClose}>
            Cancel
          </button>
          <button type="submit" className="primary" disabled={busy}>
            Start login
          </button>
        </div>
      </form>
    );
  }

  const link = safeLoginUrl(login.verification_url);
  return (
    <div>
      {login.state === "pending" && (
        <>
          <ol className="small">
            <li>
              Open{" "}
              {link ? (
                <a href={link} target="_blank" rel="noopener noreferrer">
                  the OpenAI sign-in page
                </a>
              ) : (
                "the sign-in page (the link was refused)"
              )}
              .
            </li>
            <li>
              Enter this code: <code aria-label="One-time code">{login.user_code}</code>
            </li>
            <li>Come back here; this page updates by itself.</li>
          </ol>
          <p className="muted small" role="status">
            Waiting for sign-in… expires in {Math.ceil(login.expires_in_s / 60)} min.
          </p>
        </>
      )}
      {login.state === "completed" && (
        <p role="status">The account was created and is ready to lease.</p>
      )}
      {login.state !== "pending" && login.state !== "completed" && (
        <p className="error" role="alert">
          Login {login.state}
          {login.error ? ` (${login.error})` : ""}. Nothing was stored.
        </p>
      )}
      {error && <p className="error">{error}</p>}
      <div className="actions">
        <button type="button" onClick={cancel}>
          {login.state === "pending" ? "Cancel login" : "Close"}
        </button>
      </div>
    </div>
  );
}
