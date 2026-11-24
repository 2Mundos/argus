// @vitest-environment jsdom
import { beforeEach, describe, expect, it } from "vitest";
import indexHtml from "../index.html?raw";
import manifestRaw from "../public/site.webmanifest?raw";
import swSource from "../public/sw.js?raw";
import {
  DISMISS_DAYS,
  dismiss,
  installMode,
  isDismissed,
  isIosSafari,
  isMobileUserAgent,
  markInstalled,
} from "./pwaInstall";

const ANDROID =
  "Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 Chrome/126.0 Mobile Safari/537.36";
const IPHONE =
  "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15 Version/17.5 Mobile/15E148 Safari/604.1";
const IOS_CHROME =
  "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15 CriOS/126.0 Mobile/15E148 Safari/604.1";
const DESKTOP = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/126.0 Safari/537.36";
const DAY = 24 * 60 * 60 * 1000;
const NOW = 1_700_000_000_000;

beforeEach(() => localStorage.clear());

const mode = (over: Partial<Parameters<typeof installMode>[0]>) =>
  installMode({
    ua: ANDROID,
    standalone: false,
    storage: localStorage,
    now: NOW,
    hasInstallEvent: true,
    ...over,
  });

describe("user agent detection", () => {
  it("recognises mobile and desktop agents", () => {
    expect(isMobileUserAgent(ANDROID)).toBe(true);
    expect(isMobileUserAgent(IPHONE)).toBe(true);
    expect(isMobileUserAgent(DESKTOP)).toBe(false);
  });
  it("tells iOS Safari from iOS Chrome", () => {
    expect(isIosSafari(IPHONE)).toBe(true);
    expect(isIosSafari(IOS_CHROME)).toBe(false);
    expect(isIosSafari(ANDROID)).toBe(false);
  });
});

describe("install mode", () => {
  it("uses the install event on Android and manual steps on iOS Safari", () => {
    expect(mode({})).toBe("prompt");
    expect(mode({ ua: IPHONE, hasInstallEvent: false })).toBe("ios");
  });
  it("stays hidden on desktop, in standalone mode, and without an install event elsewhere", () => {
    expect(mode({ ua: DESKTOP })).toBe("none");
    expect(mode({ standalone: true })).toBe("none");
    expect(mode({ hasInstallEvent: false })).toBe("none");
  });
});

describe("dismissal", () => {
  it("is remembered for the configured period only", () => {
    dismiss(localStorage, NOW);
    expect(mode({})).toBe("none");
    expect(isDismissed(localStorage, NOW + (DISMISS_DAYS - 1) * DAY)).toBe(true);
    expect(isDismissed(localStorage, NOW + DISMISS_DAYS * DAY + 1)).toBe(false);
  });
  it("is permanent once installed", () => {
    markInstalled(localStorage);
    expect(isDismissed(localStorage, NOW + 1000 * DAY)).toBe(true);
    expect(mode({ now: NOW + 1000 * DAY })).toBe("none");
  });
});

describe("web app manifest", () => {
  const manifest = JSON.parse(manifestRaw);
  it("has the installability fields", () => {
    expect(manifest.name).toBe("Argus");
    expect(manifest.short_name).toBe("Argus");
    expect(manifest.start_url).toBe("/");
    expect(manifest.display).toBe("standalone");
    expect(manifest.theme_color).toMatch(/^#[0-9a-f]{6}$/i);
    expect(manifest.background_color).toMatch(/^#[0-9a-f]{6}$/i);
  });
  it("declares 192, 512 and maskable icons that exist", () => {
    const icons = manifest.icons as { src: string; sizes: string; purpose?: string }[];
    expect(icons.some((i) => i.sizes === "192x192")).toBe(true);
    expect(icons.some((i) => i.sizes === "512x512" && i.purpose === "any")).toBe(true);
    expect(icons.some((i) => i.purpose === "maskable")).toBe(true);
    const files = Object.keys(import.meta.glob("../public/*.png"));
    for (const icon of icons) expect(files).toContain(`../public${icon.src}`);
  });
  it("is linked from index.html", () => {
    expect(indexHtml).toContain('rel="manifest" href="/site.webmanifest"');
  });
});

describe("service worker", () => {
  const sw = swSource;
  it("never caches API, auth or health requests", () => {
    for (const p of ["/api/", "/healthz", "/auth", "/login"]) expect(sw).toContain(`"${p}"`);
    expect(sw).toContain('request.method !== "GET"');
  });
  it("falls back to the offline page on failed navigations", () => {
    expect(sw).toContain('caches.match("/offline.html")');
  });
});
