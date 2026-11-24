import { useEffect, useState } from "react";
import { dismiss, type InstallMode, installMode, isStandalone, markInstalled } from "./pwaInstall";

type InstallEvent = Event & {
  prompt: () => Promise<void>;
  userChoice: Promise<{ outcome: "accepted" | "dismissed" }>;
};

export function InstallPrompt() {
  const [event, setEvent] = useState<InstallEvent | null>(null);
  const [mode, setMode] = useState<InstallMode>("none");

  useEffect(() => {
    const evaluate = (e: InstallEvent | null) =>
      setMode(
        installMode({
          ua: navigator.userAgent,
          maxTouchPoints: navigator.maxTouchPoints,
          standalone: isStandalone(window),
          storage: localStorage,
          now: Date.now(),
          hasInstallEvent: e !== null,
        }),
      );
    const onPrompt = (e: Event) => {
      e.preventDefault();
      setEvent(e as InstallEvent);
      evaluate(e as InstallEvent);
    };
    const onInstalled = () => {
      markInstalled(localStorage);
      setEvent(null);
      setMode("none");
    };
    evaluate(null);
    window.addEventListener("beforeinstallprompt", onPrompt);
    window.addEventListener("appinstalled", onInstalled);
    return () => {
      window.removeEventListener("beforeinstallprompt", onPrompt);
      window.removeEventListener("appinstalled", onInstalled);
    };
  }, []);

  if (mode === "none") return null;

  const close = () => {
    dismiss(localStorage, Date.now());
    setMode("none");
  };
  const install = async () => {
    if (!event) return;
    await event.prompt();
    const choice = await event.userChoice;
    if (choice.outcome === "accepted") markInstalled(localStorage);
    else dismiss(localStorage, Date.now());
    setEvent(null);
    setMode("none");
  };

  return (
    <aside className="install-banner" role="region" aria-label="Install Argus">
      <div className="install-text">
        <strong>Install Argus</strong>
        {mode === "ios" ? (
          <span>Tap Share, then Add to Home Screen.</span>
        ) : (
          <span>Add the dashboard to your home screen.</span>
        )}
      </div>
      {mode === "prompt" && (
        <button type="button" className="primary" onClick={install}>
          Install
        </button>
      )}
      <button type="button" onClick={close} aria-label="Dismiss install prompt">
        Not now
      </button>
    </aside>
  );
}
