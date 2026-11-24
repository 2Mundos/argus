export const DISMISS_KEY = "argus.pwa-install-dismissed-at";
export const DISMISS_DAYS = 14;
const INSTALLED_KEY = "argus.pwa-installed";
const DAY_MS = 24 * 60 * 60 * 1000;

export type InstallMode = "prompt" | "ios" | "none";

export function isMobileUserAgent(ua: string): boolean {
  return /Android|iPhone|iPad|iPod|Mobile/i.test(ua);
}

export function isIosSafari(ua: string, maxTouchPoints = 0): boolean {
  // iPadOS 13+ reports a desktop Mac user agent; touch points tell it apart.
  const ios = /iPhone|iPad|iPod/i.test(ua) || (/Macintosh/i.test(ua) && maxTouchPoints > 1);
  return ios && /Safari/i.test(ua) && !/CriOS|FxiOS|EdgiOS|OPiOS/i.test(ua);
}

export function isStandalone(win: Window): boolean {
  const nav = win.navigator as Navigator & { standalone?: boolean };
  return Boolean(win.matchMedia?.("(display-mode: standalone)").matches || nav.standalone === true);
}

export function markInstalled(storage: Storage): void {
  storage.setItem(INSTALLED_KEY, "1");
}

export function dismiss(storage: Storage, now: number): void {
  storage.setItem(DISMISS_KEY, String(now));
}

export function isDismissed(storage: Storage, now: number): boolean {
  if (storage.getItem(INSTALLED_KEY) === "1") return true;
  const at = Number(storage.getItem(DISMISS_KEY));
  return Number.isFinite(at) && at > 0 && now - at < DISMISS_DAYS * DAY_MS;
}

/** What the banner should offer; "none" when it must stay hidden. */
export function installMode(opts: {
  ua: string;
  maxTouchPoints?: number;
  standalone: boolean;
  storage: Storage;
  now: number;
  hasInstallEvent: boolean;
}): InstallMode {
  if (!isMobileUserAgent(opts.ua) && !isIosSafari(opts.ua, opts.maxTouchPoints)) return "none";
  if (opts.standalone) return "none";
  if (isDismissed(opts.storage, opts.now)) return "none";
  if (opts.hasInstallEvent) return "prompt";
  if (isIosSafari(opts.ua, opts.maxTouchPoints)) return "ios";
  return "none";
}

export function registerServiceWorker(): void {
  if (!("serviceWorker" in navigator) || import.meta.env.DEV) return;
  window.addEventListener("load", () => {
    navigator.serviceWorker.register("/sw.js").catch(() => undefined);
  });
}
