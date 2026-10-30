import { useCallback, useEffect, useState } from "react";
import { api, call, type OnboardingStatus } from "../api";
import { useLiveData } from "../live";
import { ClaudeAccount } from "./ClaudeAccount";
import { CodexAccount } from "./CodexAccount";
import { NewWorker } from "./GuidedWorker";

type Step = 1 | 2 | 3;

export function shouldAutoOpen(status: OnboardingStatus | undefined): boolean {
  if (!status || status.dismissed) return false;
  return !status.account || !status.worker;
}

export function firstIncompleteStep(status: OnboardingStatus): Step {
  if (!status.account) return 1;
  if (!status.worker) return 2;
  if (!status.orchestrator) return 3;
  return 3;
}

async function dismissOnboarding(): Promise<void> {
  await call(
    api.PUT("/api/settings/{key}", {
      params: { path: { key: "onboarding.dismissed" } },
      body: { value: true },
    }),
  );
}

export function OnboardingWizard({
  open,
  initialStep,
  onClose,
}: {
  open: boolean;
  initialStep?: Step;
  onClose: () => void;
}) {
  const { data: status, reload } = useLiveData(
    (signal) => call(api.GET("/api/onboarding", { signal })),
    [],
  );
  const [step, setStep] = useState<Step>(initialStep ?? 1);
  const [addingClaude, setAddingClaude] = useState(false);
  const [addingCodex, setAddingCodex] = useState(false);
  const [error, setError] = useState<string>();
  const [budget, setBudget] = useState("5");
  const [enabling, setEnabling] = useState(false);

  useEffect(() => {
    if (!open || !status) return;
    setStep(initialStep ?? firstIncompleteStep(status));
  }, [open, status, initialStep]);

  // Enabling ends when the refreshed status shows the orchestrator (or the request fails),
  // not when the PUTs return: until then the panel shows progress, not the form again.
  const orchestratorOn = status?.orchestrator === true;
  useEffect(() => {
    if (orchestratorOn) setEnabling(false);
  }, [orchestratorOn]);
  useEffect(() => {
    if (!enabling) return;
    const t = setTimeout(() => {
      setEnabling(false);
      setError(
        "The orchestrator was enabled, but its status has not updated yet. Reopen to check.",
      );
    }, 20_000);
    return () => clearTimeout(t);
  }, [enabling]);

  useEffect(() => {
    if (!status) return;
    if (step === 1 && status.account) setStep(2);
    if (step === 2 && status.worker && status.account) setStep(3);
  }, [status, step]);

  const close = useCallback(async () => {
    setError(undefined);
    try {
      await dismissOnboarding();
      onClose();
    } catch (err) {
      setError((err as Error).message);
    }
  }, [onClose]);

  if (!open) return null;

  const accountDone = status?.account === true;
  const workerDone = status?.worker === true;
  const orchDone = status?.orchestrator === true;

  async function enableOrchestrator() {
    setEnabling(true);
    setError(undefined);
    try {
      const value = Number(budget);
      if (!Number.isFinite(value) || value < 0) {
        setError("Daily budget must be a number ≥ 0.");
        setEnabling(false);
        return;
      }
      await call(
        api.PUT("/api/settings/{key}", {
          params: { path: { key: "orchestrator.daily_budget_usd" } },
          body: { value },
        }),
      );
      await call(
        api.PUT("/api/settings/{key}", {
          params: { path: { key: "orchestrator.enabled" } },
          body: { value: true },
        }),
      );
      reload();
    } catch (err) {
      setError((err as Error).message);
      setEnabling(false);
    }
  }

  return (
    <>
      <div className="overlay onboarding-overlay">
        <div
          className="dialog wide onboarding-dialog"
          role="dialog"
          aria-modal="true"
          aria-labelledby="onboarding-title"
        >
          <div className="toolbar">
            <h2 id="onboarding-title">Set up Argus</h2>
            <button type="button" className="link" onClick={() => void close()}>
              Close
            </button>
          </div>
          <p className="muted small">
            Three short steps to a working install. Close anytime; reopen from the Dashboard.
          </p>
          <ol className="onboarding-steps" aria-label="Onboarding steps">
            <StepTab
              n={1}
              label="Add an account"
              done={accountDone}
              current={step === 1}
              onPick={() => setStep(1)}
            />
            <StepTab
              n={2}
              label="Create a first worker"
              done={workerDone}
              current={step === 2}
              onPick={() => setStep(2)}
            />
            <StepTab
              n={3}
              label="Start the orchestrator"
              done={orchDone}
              current={step === 3}
              onPick={() => setStep(3)}
            />
          </ol>
          {error && <p className="error">{error}</p>}
          {step === 1 && (
            <AccountStep
              done={accountDone}
              onClaude={() => setAddingClaude(true)}
              onCodex={() => setAddingCodex(true)}
              onSkip={() => setStep(2)}
            />
          )}
          {step === 2 && (
            <WorkerStep done={workerDone} onDone={() => void reload()} onSkip={() => setStep(3)} />
          )}
          {step === 3 && (
            <OrchestratorStep
              done={orchDone}
              budget={budget}
              setBudget={setBudget}
              enabling={enabling}
              onEnable={() => void enableOrchestrator()}
              onSkip={() => void close()}
            />
          )}
        </div>
      </div>
      {addingClaude && (
        <ClaudeAccount
          onClose={() => setAddingClaude(false)}
          onDone={() => {
            setAddingClaude(false);
            void reload();
          }}
        />
      )}
      {addingCodex && (
        <CodexAccount
          onClose={() => setAddingCodex(false)}
          onDone={() => {
            setAddingCodex(false);
            void reload();
          }}
        />
      )}
    </>
  );
}

function StepTab(props: {
  n: number;
  label: string;
  done: boolean;
  current: boolean;
  onPick: () => void;
}) {
  return (
    <li>
      <button
        type="button"
        className={props.current ? "primary" : ""}
        aria-current={props.current ? "step" : undefined}
        onClick={props.onPick}
      >
        <span className="onboarding-step-n">{props.done ? "✓" : props.n}</span> {props.label}
        {props.done && !props.current && <span className="muted small"> done</span>}
      </button>
    </li>
  );
}

function AccountStep(props: {
  done: boolean;
  onClaude: () => void;
  onCodex: () => void;
  onSkip: () => void;
}) {
  return (
    <div className="onboarding-panel">
      <h3>Add an account</h3>
      <p>
        Workers need a harness-capable account. Claude and Codex can carry a worker on their own;
        other providers (Cursor, Copilot, custom) are available from{" "}
        <a href="#setup/accounts">Accounts</a>.
      </p>
      {props.done ? (
        <p className="small">An account is ready. Continue to create a worker, or skip ahead.</p>
      ) : (
        <div className="toolbar">
          <button type="button" className="primary" onClick={props.onClaude}>
            Add Claude account
          </button>
          <button type="button" className="primary" onClick={props.onCodex}>
            Add Codex account
          </button>
        </div>
      )}
      <div className="toolbar">
        <button type="button" onClick={props.onSkip}>
          {props.done ? "Continue" : "Skip for now"}
        </button>
      </div>
    </div>
  );
}

function WorkerStep(props: { done: boolean; onDone: () => void; onSkip: () => void }) {
  return (
    <div className="onboarding-panel">
      <h3>Create a first worker</h3>
      <p>Pick a template for this node. The guided form is the same as on the Agents page.</p>
      {props.done ? (
        <p className="small">A worker pool exists. Continue to the orchestrator, or skip.</p>
      ) : (
        <NewWorker onDone={props.onDone} />
      )}
      <div className="toolbar">
        <button type="button" onClick={props.onSkip}>
          {props.done ? "Continue" : "Skip for now"}
        </button>
      </div>
    </div>
  );
}

function OrchestratorStep(props: {
  done: boolean;
  budget: string;
  setBudget: (v: string) => void;
  enabling: boolean;
  onEnable: () => void;
  onSkip: () => void;
}) {
  return (
    <div className="onboarding-panel">
      <h3>Start the internal orchestrator</h3>
      <p>
        Argus hosts a NanoClaw orchestrator as a system pool (size 1). Chat with it becomes the main
        page. It spends against a daily USD budget; disable anytime in Settings.
      </p>
      {props.done ? (
        <p className="small">
          Orchestrator enabled. Open <a href="#chat">Chat</a> to send a goal.
        </p>
      ) : props.enabling ? (
        <p className="busy" role="status" aria-live="polite">
          <span className="spinner" aria-hidden="true" />
          Enabling the orchestrator… starting its container can take a moment.
        </p>
      ) : (
        <>
          <label>
            Daily budget (USD){" "}
            <input
              type="number"
              min={0}
              step="0.5"
              value={props.budget}
              onChange={(e) => props.setBudget(e.target.value)}
            />
          </label>
          <button type="button" className="primary" onClick={props.onEnable}>
            Enable orchestrator
          </button>
        </>
      )}
      <div className="toolbar">
        <button type="button" onClick={props.onSkip}>
          {props.done ? "Done" : "Skip for now"}
        </button>
      </div>
    </div>
  );
}

/** Hosts auto-open and the Set up entry points. */
export function OnboardingHost({
  requestOpen,
  onRequestHandled,
}: {
  requestOpen: Step | null;
  onRequestHandled: () => void;
}) {
  const { data: status, reload } = useLiveData(
    (signal) => call(api.GET("/api/onboarding", { signal })),
    [],
  );
  const [open, setOpen] = useState(false);
  const [step, setStep] = useState<Step | undefined>();
  const [autoTried, setAutoTried] = useState(false);

  useEffect(() => {
    if (autoTried || !status) return;
    setAutoTried(true);
    if (shouldAutoOpen(status)) {
      setStep(firstIncompleteStep(status));
      setOpen(true);
    }
  }, [status, autoTried]);

  useEffect(() => {
    if (requestOpen === null) return;
    setStep(requestOpen);
    setOpen(true);
    onRequestHandled();
  }, [requestOpen, onRequestHandled]);

  return (
    <OnboardingWizard
      open={open}
      initialStep={step}
      onClose={() => {
        setOpen(false);
        void reload();
      }}
    />
  );
}
