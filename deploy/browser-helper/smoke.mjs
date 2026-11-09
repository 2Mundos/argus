// Sandbox-ON Chromium smoke over raw CDP (Node 22 builtin fetch/WebSocket); no Playwright.
// Runs INSIDE the hardened container started by scripts/browser_helper_smoke.py.
// Args: OUTDIR (fresh, writable). Never passes --no-sandbox or any other sandbox-disabling flag.
// Fail closed: any exception or the global deadline writes result.json with all_pass=false, exit 3.

import { spawn } from "node:child_process";
import crypto from "node:crypto";
import fs from "node:fs";
import http from "node:http";

const out = process.argv[2];
if (!out) {
  console.error("usage: smoke.mjs OUTDIR");
  process.exit(2);
}
const DEADLINE_MS = 80_000;
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const sha = (b) => crypto.createHash("sha256").update(b).digest("hex");
// Strict read: an unreadable /proc file of a live process must fail the run, never be skipped.
const rd = (p) => fs.readFileSync(p, "utf8");

// No stale artefact can satisfy a check.
for (const f of ["downloads", "screenshot.png", "result.json"]) {
  fs.rmSync(`${out}/${f}`, { recursive: true, force: true });
}
fs.mkdirSync(`${out}/downloads`, { recursive: true });

const DL = Buffer.from("ARGUS_DOWNLOAD_SMOKE\n".repeat(50));
const srv = http.createServer((req, res) => {
  if (req.url === "/dl.bin") {
    res.writeHead(200, {
      "content-type": "application/octet-stream",
      "content-disposition": 'attachment; filename="dl.bin"',
    });
    res.end(DL);
    return;
  }
  res.writeHead(200, { "content-type": "text/html" });
  res.end(
    '<!doctype html><title>t</title><h1 id=h style="color:#fff;background:#06c">ARGUS_SAFE_RENDER</h1>' +
      '<script>document.getElementById("h").dataset.js=String(6*7)</script>',
  );
});

const args = [
  "--headless=new",
  "--disable-gpu",
  "--remote-debugging-port=9333",
  "--user-data-dir=/tmp/chrome-profile",
  "--no-first-run",
  "--disable-crash-reporter",
  "about:blank",
];
const result = { chromium_argv: ["chromium", ...args], steps: {}, assertions: [], all_pass: false };
let ch = null;
let errlog = "";

function finish(code) {
  try {
    fs.writeFileSync(`${out}/result.json`, JSON.stringify(result, null, 2));
  } catch {}
  try {
    ch?.kill("SIGKILL");
  } catch {}
  process.exit(code);
}
setTimeout(() => {
  result.error = `deadline ${DEADLINE_MS}ms exceeded`;
  finish(3);
}, DEADLINE_MS).unref();

async function run() {
  await new Promise((r) => srv.listen(0, "127.0.0.1", r));
  const base = `http://127.0.0.1:${srv.address().port}`;
  ch = spawn("chromium", args, { stdio: ["ignore", "ignore", "pipe"] });
  ch.stderr.on("data", (d) => {
    if (errlog.length < 65536) errlog += d;
  });

  let ver = null;
  for (let i = 0; i < 100 && !ver; i++) {
    try {
      ver = await (await fetch("http://127.0.0.1:9333/json/version")).json();
    } catch {
      await sleep(200);
    }
  }
  if (!ver) throw new Error("no devtools endpoint");
  result.version = { Browser: ver.Browser, UA: ver["User-Agent"] };

  const ws = new WebSocket(ver.webSocketDebuggerUrl);
  await new Promise((r) => {
    ws.onopen = r;
  });
  let id = 0;
  const pend = new Map();
  const events = [];
  ws.onmessage = (m) => {
    const d = JSON.parse(m.data);
    if (d.id && pend.has(d.id)) {
      pend.get(d.id)(d);
      pend.delete(d.id);
    } else events.push(d);
  };
  const send = (method, params = {}, sessionId) =>
    new Promise((r) => {
      const i = ++id;
      pend.set(i, r);
      ws.send(JSON.stringify({ id: i, method, params, sessionId }));
    });

  const tc = (await send("Target.createTarget", { url: "about:blank" })).result;
  const sid = (await send("Target.attachToTarget", { targetId: tc.targetId, flatten: true })).result
    .sessionId;
  await send("Page.enable", {}, sid);
  await send("Browser.setDownloadBehavior", {
    behavior: "allow",
    downloadPath: `${out}/downloads`,
    eventsEnabled: true,
  });
  await send("Page.navigate", { url: `${base}/` }, sid);
  await sleep(1500);
  const ev = await send(
    "Runtime.evaluate",
    {
      expression:
        'JSON.stringify({text:document.getElementById("h").textContent,js:document.getElementById("h").dataset.js,outer:document.documentElement.outerHTML.length})',
      returnByValue: true,
    },
    sid,
  );
  const dom = JSON.parse(ev.result.result.value);
  result.steps.dom_js = { ...dom, pass: dom.text === "ARGUS_SAFE_RENDER" && dom.js === "42" };

  const shot = await send("Page.captureScreenshot", { format: "png" }, sid);
  const png = Buffer.from(shot.result.data, "base64");
  fs.writeFileSync(`${out}/screenshot.png`, png);
  result.steps.screenshot = {
    bytes: png.length,
    sha256: sha(png),
    png_magic: png.subarray(0, 8).toString("hex"),
  };

  await send(
    "Runtime.evaluate",
    {
      expression:
        "(()=>{const a=document.createElement('a');a.href='/dl.bin';a.download='dl.bin';document.body.appendChild(a);a.click();})()",
    },
    sid,
  );
  for (let i = 0; i < 50; i++) {
    if (
      events.some((e) => e.method === "Browser.downloadProgress" && e.params.state === "completed")
    )
      break;
    await sleep(200);
  }
  const dlp = `${out}/downloads/`;
  const files = fs.readdirSync(dlp);
  const got = files.map((f) => ({ f, sha256: sha(fs.readFileSync(dlp + f)) }));
  result.steps.download = {
    files,
    expected_sha256: sha(DL),
    got,
    pass: got.some((g) => g.sha256 === sha(DL)),
  };

  // chrome://sandbox text and per-process /proc evidence.
  const t2 = await send("Target.createTarget", { url: "chrome://sandbox" });
  const a2 = await send("Target.attachToTarget", { targetId: t2.result.targetId, flatten: true });
  await sleep(1500);
  const sb = await send(
    "Runtime.evaluate",
    { expression: "document.body.innerText", returnByValue: true },
    a2.result.sessionId,
  );
  result.steps.chrome_sandbox_page = sb.result.result?.value;

  const procs = [];
  const inspectErrors = [];
  for (const p of fs.readdirSync("/proc").filter((x) => /^\d+$/.test(x))) {
    try {
      const argv = rd(`/proc/${p}/cmdline`)
        .split("\0")
        .filter((x) => x !== "");
      if (!/chrom/.test(argv[0] || "")) continue;
      const st = rd(`/proc/${p}/status`);
      const g = (k) => (st.match(new RegExp(`^${k}:\\s*(.*)$`, "m")) || [])[1];
      procs.push({
        pid: p,
        type: (argv.join(" ").match(/--type=(\S+)/) || [0, "browser"])[1],
        argv,
        uid: g("Uid"),
        gid: g("Gid"),
        apparmor: rd(`/proc/${p}/attr/current`).trim(),
        userns: fs.readlinkSync(`/proc/${p}/ns/user`),
        netns: fs.readlinkSync(`/proc/${p}/ns/net`),
        pidns: fs.readlinkSync(`/proc/${p}/ns/pid`),
        uid_map: rd(`/proc/${p}/uid_map`).trim().replace(/\s+/g, " "),
        gid_map: rd(`/proc/${p}/gid_map`).trim().replace(/\s+/g, " "),
        CapEff: g("CapEff"),
        CapBnd: g("CapBnd"),
        NoNewPrivs: g("NoNewPrivs"),
        Seccomp: g("Seccomp"),
      });
    } catch (e) {
      // Only a process that is really gone may be skipped; anything else is a failed inspection.
      if (e.code === "ENOENT" && !fs.existsSync(`/proc/${p}`)) continue;
      inspectErrors.push({ pid: p, code: e.code || String(e) });
    }
  }
  result.steps.processes = procs;
  result.steps.inspect_errors = inspectErrors;

  const browserUserns = procs.find((p) => p.type === "browser")?.userns;
  const bad =
    /--no-sandbox|--disable-setuid-sandbox|--disable-seccomp-filter-sandbox|--disable-namespace-sandbox/;
  const zero = /^0+$/;
  const A = result.assertions;
  const chk = (name, ok, detail) => A.push({ name, ok: !!ok, detail });
  const nested = procs.filter((p) => p.userns !== browserUserns);
  const outer = procs.filter((p) => p.userns === browserUserns);
  const sbpage = result.steps.chrome_sandbox_page || "";
  chk("dom_js", result.steps.dom_js.pass);
  chk(
    "screenshot_png",
    result.steps.screenshot.png_magic === "89504e470d0a1a0a" &&
      result.steps.screenshot.bytes > 1000,
    result.steps.screenshot.sha256,
  );
  chk("download_hash", result.steps.download.pass, result.steps.download.expected_sha256);
  chk(
    "chrome_sandbox_adequate",
    /You are adequately sandboxed/.test(sbpage) && /Layer 1 Sandbox\tNamespace/.test(sbpage),
  );
  chk(
    "have_browser_renderer_zygote",
    ["browser", "renderer", "zygote"].every((t) => procs.some((p) => p.type === t)),
    procs.map((p) => p.type).join(","),
  );
  // Uid/Gid lines hold real, effective, saved and filesystem ids; all four must be 1000.
  const allIds1000 = (v) => /^1000\s+1000\s+1000\s+1000$/.test(v || "");
  chk("all_live_procs_inspected", inspectErrors.length === 0, JSON.stringify(inspectErrors));
  chk("every_proc_has_argv", procs.length > 0 && procs.every((p) => p.argv.length > 0));
  chk("launch_argv_no_bypass_flag", !bad.test(args.join(" ")));
  chk("no_bypass_flag_in_any_argv", !procs.some((p) => bad.test(p.argv.join(" "))));
  chk(
    "all_uid_gid_1000",
    procs.every((p) => allIds1000(p.uid) && allIds1000(p.gid)),
    procs.map((p) => p.uid).join("|"),
  );
  chk(
    "all_apparmor_enforce",
    procs.every((p) => p.apparmor === "docker-default (enforce)"),
    [...new Set(procs.map((p) => p.apparmor))].join("|"),
  );
  chk(
    "all_nonewprivs_seccomp2",
    procs.every((p) => p.NoNewPrivs === "1" && p.Seccomp === "2"),
  );
  chk(
    "container_userns_procs_zero_caps",
    outer.length > 0 && outer.every((p) => zero.test(p.CapEff) && zero.test(p.CapBnd)),
  );
  chk("nested_userns_exist", nested.length > 0, `${nested.length} procs`);
  chk(
    "renderers_nested_zero_caps",
    procs.some((p) => p.type === "renderer") &&
      procs
        .filter((p) => p.type === "renderer")
        .every((p) => p.userns !== browserUserns && zero.test(p.CapEff)),
  );
  chk(
    "nested_caps_only_sys_chroot_bit21",
    nested.every((p) => zero.test(p.CapEff) || p.CapEff === "0000000000200000"),
    nested.map((p) => `${p.type}:${p.CapEff}`).join(","),
  );
  chk(
    "nested_ns_maps_single_uid_1000_to_inner",
    nested.every((p) => /^\d+ 1000 1$/.test(p.uid_map)),
    nested.map((p) => `${p.type} uid_map=${p.uid_map}`).join(";"),
  );
  chk(
    "outer_userns_map_recorded",
    outer.length > 0 && outer.every((p) => p.uid_map !== "" && p.gid_map !== ""),
    [...new Set(outer.map((p) => p.uid_map))].join("|"),
  );
  chk("no_sandbox_stderr_errors", !/No usable sandbox|Zygote process exited/.test(errlog));
  result.all_pass = A.every((a) => a.ok);
}

try {
  await run();
} catch (e) {
  result.error = String(e?.stack || e);
  result.all_pass = false;
}
console.log(JSON.stringify(result.assertions, null, 1));
console.log(`ALL_PASS=${result.all_pass}`);
if (result.error) console.error(result.error);
finish(result.all_pass ? 0 : 3);
