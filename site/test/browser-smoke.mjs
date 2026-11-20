/** Optional local responsive check using a disposable Chrome profile. */
import { spawn } from "node:child_process";
import { access, mkdtemp, rm, writeFile } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const site = path.dirname(path.dirname(fileURLToPath(import.meta.url)));
const candidates = [process.env.CHROME_PATH, "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome", "/usr/bin/google-chrome", "/usr/bin/chromium"];
let binary;
for (const candidate of candidates.filter(Boolean)) {
  try { await access(candidate); binary = candidate; break; } catch { /* Try the next location. */ }
}
if (!binary) throw new Error("Chrome not found; set CHROME_PATH to run the optional browser check.");

const profile = await mkdtemp(path.join(os.tmpdir(), "ai-observatory-browser-"));
const chrome = spawn(binary, [
  "--headless=new", "--disable-gpu", "--disable-background-networking", "--disable-sync", "--disable-extensions", "--no-first-run",
  "--remote-debugging-port=0", `--user-data-dir=${profile}`, "about:blank",
], { stdio: ["ignore", "ignore", "pipe"] });
const exited = new Promise((resolve) => chrome.once("exit", resolve));
let socket;

function timeout(promise, label, milliseconds = 20000) {
  let timer;
  return Promise.race([
    promise,
    new Promise((_, reject) => { timer = setTimeout(() => reject(new Error(`${label} timed out`)), milliseconds); }),
  ]).finally(() => clearTimeout(timer));
}

try {
  const endpoint = await timeout(new Promise((resolve, reject) => {
    let buffer = "";
    chrome.stderr.on("data", (data) => {
      buffer += data;
      const match = buffer.match(/DevTools listening on (ws:\/\/[^\s]+)/);
      if (match) resolve(match[1]);
    });
    chrome.once("exit", () => reject(new Error("Browser exited before startup")));
    chrome.once("error", reject);
  }), "Browser startup");
  socket = new WebSocket(endpoint);
  await timeout(new Promise((resolve, reject) => {
    socket.addEventListener("open", resolve, { once: true });
    socket.addEventListener("error", reject, { once: true });
  }), "Browser connection");
  let id = 0;
  const pending = new Map();
  const events = [];
  socket.addEventListener("message", (event) => {
    const message = JSON.parse(event.data);
    if (message.id && pending.has(message.id)) {
      const { resolve, reject } = pending.get(message.id);
      pending.delete(message.id);
      message.error ? reject(new Error(message.error.message)) : resolve(message.result);
    }
    if (message.method) {
      for (const item of [...events]) {
        if (item.method === message.method && item.session === message.sessionId) {
          events.splice(events.indexOf(item), 1);
          item.resolve(message.params);
        }
      }
    }
  });
  function send(method, params = {}, session) {
    const request = { id: ++id, method, params };
    if (session) request.sessionId = session;
    socket.send(JSON.stringify(request));
    return timeout(new Promise((resolve, reject) => pending.set(request.id, { resolve, reject })), method);
  }
  function waitFor(method, session) {
    return timeout(new Promise((resolve) => events.push({ method, session, resolve })), method);
  }
  const target = await send("Target.createTarget", { url: "about:blank" });
  const { sessionId } = await send("Target.attachToTarget", { targetId: target.targetId, flatten: true });
  await send("Page.enable", {}, sessionId);
  for (const [label, width, file] of [
    ["home-mobile", 390, "index.html"], ["home-narrow", 320, "index.html"],
    ["guide-mobile", 390, "guide/index.html"], ["home-desktop", 1440, "index.html"],
  ]) {
    await send("Emulation.setDeviceMetricsOverride", { width, height: 1050, deviceScaleFactor: 1, mobile: width < 600 }, sessionId);
    const loaded = waitFor("Page.loadEventFired", sessionId);
    await send("Page.navigate", { url: pathToFileURL(path.join(site, "dist", file)).href }, sessionId);
    await loaded;
    await send("Runtime.evaluate", {
      expression: "new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))",
      awaitPromise: true,
    }, sessionId);
    const evaluation = await send("Runtime.evaluate", {
      expression: `JSON.stringify({ viewport: innerWidth, scroll: document.documentElement.scrollWidth,
        clipped: [...document.querySelectorAll('nav a, main a, .trace-card, .hero-visual, h1')]
          .filter((element) => { const r = element.getBoundingClientRect(); const s = getComputedStyle(element);
            return s.display !== 'none' && r.width && (r.left < -1 || r.right > innerWidth + 1); })
          .map((element) => element.tagName + '.' + element.className) })`,
      returnByValue: true,
    }, sessionId);
    const metrics = JSON.parse(evaluation.result.value);
    if (metrics.viewport !== width || metrics.scroll > width || metrics.clipped.length) {
      throw new Error(`${label} has horizontal overflow: ${JSON.stringify(metrics)}`);
    }
    if (process.env.SITE_SCREENSHOT_DIR) {
      const screenshot = await send("Page.captureScreenshot", { format: "png" }, sessionId);
      await writeFile(path.join(process.env.SITE_SCREENSHOT_DIR, `${label}.png`), Buffer.from(screenshot.data, "base64"), { flag: "wx" });
    }
    console.log(`${label}: ${width}px viewport, no horizontal overflow`);
  }
} finally {
  if (socket?.readyState === WebSocket.OPEN) {
    socket.send(JSON.stringify({ id: 999999, method: "Browser.close", params: {} }));
    await timeout(exited, "Browser shutdown", 5000).catch(() => chrome.kill());
  } else {
    chrome.kill();
  }
  await timeout(exited, "Browser exit", 5000).catch(() => {});
  socket?.close();
  await rm(profile, { recursive: true, force: true, maxRetries: 10, retryDelay: 200 });
}
