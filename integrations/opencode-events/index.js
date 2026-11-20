/** Optional structural OpenCode events. No prompt, tool output or error text is exported. */
import { createHash } from "node:crypto";
import { constants } from "node:fs";
import { lstat, open } from "node:fs/promises";
import { request as httpRequest } from "node:http";
import { request as httpsRequest } from "node:https";
import { homedir } from "node:os";
import { isAbsolute, join } from "node:path";

const environment = "ai-observatory-opencode-events";
const types = [
  "session.execution.started", "session.execution.succeeded", "session.execution.failed", "session.execution.interrupted",
  "session.compaction.started", "session.compaction.ended", "session.compaction.failed",
];
const seenKey = Symbol.for("ai-observatory.structural-events.seen.v1");
const identifier = /^[A-Za-z0-9_-]{1,160}$/;
const hash = (value) => createHash("sha256").update(value).digest("hex");
const attribute = (key, value) => ({ key, value: { stringValue: value } });

export function validateConfig(value) {
  if (!value || typeof value !== "object" || Array.isArray(value)) return false;
  if (!/^pk-lf-[A-Za-z0-9_-]+$/.test(value.publicKey ?? "")) return false;
  if (!/^sk-lf-[A-Za-z0-9_-]+$/.test(value.secretKey ?? "")) return false;
  if (typeof value.baseUrl !== "string") return false;
  let url;
  try { url = new URL(value.baseUrl); } catch { return false; }
  if (url.username || url.password || url.pathname !== "/" || url.search || url.hash) return false;
  if (!url.port || value.baseUrl !== url.origin) return false;
  return (url.protocol === "http:" && url.hostname === "127.0.0.1") ||
    (url.protocol === "https:" && url.hostname.endsWith(".ts.net"));
}

export async function readConfig(root = process.env.XDG_CONFIG_HOME || join(homedir(), ".config")) {
  if (!isAbsolute(root)) return null;
  const directory = join(root, "ai-observatory");
  let file;
  try {
    const parent = await lstat(directory);
    if (!parent.isDirectory() || parent.isSymbolicLink() || parent.uid !== process.getuid() || parent.mode & 0o077) return null;
    file = await open(join(directory, "opencode-global.json"), constants.O_RDONLY | (constants.O_NOFOLLOW || 0));
    const info = await file.stat();
    if (!info.isFile() || info.uid !== process.getuid() || info.mode & 0o077 || info.size > 8192) return null;
    const value = JSON.parse(await file.readFile({ encoding: "utf8" }));
    return validateConfig(value) ? value : null;
  } catch { return null; }
  finally { await file?.close(); }
}

export function encodeEvent(event) {
  if (!event || !types.includes(event.type) || typeof event.id !== "string" || !identifier.test(event.id)) return null;
  const session = event.data?.sessionID;
  if (typeof session !== "string" || !identifier.test(session)) return null;
  if (!Number.isSafeInteger(event.created) || event.created <= 0) return null;
  let name = event.type.replace("session.", "aiobs.");
  if (event.type === "session.execution.interrupted") {
    const reason = event.data?.reason;
    name += "." + (["user", "shutdown", "superseded", "inactivity"].includes(reason) ? reason : "unknown");
  }
  if (event.type.startsWith("session.compaction.")) {
    const reason = event.data?.reason;
    name += "." + (["auto", "manual"].includes(reason) ? reason : "unknown");
  }
  const traceId = hash(`aiobs.structural.trace.v1:${event.id}`).slice(0, 32);
  const spanId = hash(`aiobs.structural.span.v1:${event.id}`).slice(0, 16);
  if (/^0+$/.test(traceId) || /^0+$/.test(spanId)) return null;
  const nanos = String(BigInt(event.created) * 1_000_000n);
  const attributes = [attribute("langfuse.observation.type", "event"), attribute("session.id", session),
    attribute("langfuse.trace.name", "OpenCode structural event")];
  if (event.type === "session.compaction.ended" || event.type === "session.compaction.failed") {
    const tokens = event.data?.tokens;
    const fields = { input: tokens?.input, output: tokens?.output, reasoning: tokens?.reasoning,
      cache_read: tokens?.cache?.read, cache_write: tokens?.cache?.write };
    const usage = Object.fromEntries(Object.entries(fields).filter(([, value]) => Number.isSafeInteger(value) && value >= 0));
    if (Object.keys(usage).length) attributes.push(attribute("langfuse.observation.usage_details", JSON.stringify(usage)));
  }
  return { resourceSpans: [{
    resource: { attributes: [attribute("service.name", "ai-observatory-structural-events"), attribute("langfuse.environment", environment)] },
    scopeSpans: [{ scope: { name: "ai-observatory.opencode-events", version: "1" }, spans: [{
      traceId, spanId, name, kind: 1, startTimeUnixNano: nanos, endTimeUnixNano: nanos,
      attributes, status: {},
    }] }],
  }] };
}

function send(config, body, setRequest) {
  return new Promise((resolve) => {
    const url = new URL("/api/public/otel/v1/traces", config.baseUrl);
    const payload = JSON.stringify(body);
    const authorization = "Basic " + Buffer.from(`${config.publicKey}:${config.secretKey}`).toString("base64");
    const request = (url.protocol === "https:" ? httpsRequest : httpRequest)(url, {
      method: "POST", agent: false, rejectUnauthorized: true, timeout: 5000,
      headers: { Authorization: authorization, "Content-Type": "application/json", "Content-Length": Buffer.byteLength(payload) },
    }, (response) => {
      const chunks = [];
      let size = 0;
      response.on("data", (chunk) => {
        size += chunk.length;
        if (size <= 65536) chunks.push(chunk);
        else response.destroy();
      });
      response.on("error", () => resolve(false));
      response.on("aborted", () => resolve(false));
      response.on("end", () => {
        if (response.statusCode < 200 || response.statusCode >= 300 || size > 65536) return resolve(false);
        try {
          const result = JSON.parse(Buffer.concat(chunks).toString("utf8") || "{}");
          const rejected = result?.partialSuccess?.rejectedSpans ?? 0;
          resolve(rejected === 0 || rejected === "0");
        } catch { resolve(false); }
      });
    });
    setRequest(request);
    request.on("timeout", () => request.destroy());
    request.on("error", () => resolve(false));
    request.end(payload);
  });
}

async function belongs(ctx, event) {
  const own = ctx.location;
  if (!own?.directory) return false;
  let location = event.location;
  if (!location?.directory) {
    try { location = (await ctx.session.get({ sessionID: event.data.sessionID })).location; }
    catch { return false; }
  }
  return location?.directory === own.directory && (location.workspaceID ?? null) === (own.workspaceID ?? null);
}

export async function activate(ctx, options = {}) {
  if (process.env.AI_OBSERVATORY_DISABLED === "1" || process.env.AI_OBSERVATORY_EVENTS_DISABLED === "1") return;
  if (/(http|net|tls|undici|\*)/i.test(`${process.env.NODE_DEBUG ?? ""},${process.env.NODE_DEBUG_NATIVE ?? ""}`)) return;
  if (typeof ctx?.event?.subscribe !== "function" || typeof ctx?.session?.get !== "function" || !ctx?.location?.directory) {
    console.warn("[AI Observatory] Structural event capture is unavailable for this OpenCode location.");
    return;
  }
  let config;
  try { config = await (options.readConfig ?? readConfig)(); }
  catch { console.warn("[AI Observatory] Structural event capture could not read its private configuration."); return; }
  if (!validateConfig(config)) {
    console.warn("[AI Observatory] Structural event capture disabled: private credentials are missing or unsafe.");
    return;
  }
  const abort = new AbortController();
  const seen = globalThis[seenKey] instanceof Map ? globalThis[seenKey] : (globalThis[seenKey] = new Map());
  const queue = [];
  let stopped = false;
  let closing = false;
  let draining = false;
  let queueFullReported = false;
  let drainPromise = Promise.resolve();
  let activeRequest;
  const enqueue = (event, payload) => {
    if (stopped || closing || seen.has(event.id)) return;
    if (queue.length >= 128) {
      if (!queueFullReported) console.warn("[AI Observatory] Structural event queue is full; events may be missing.");
      queueFullReported = true;
      return;
    }
    seen.set(event.id, true);
    if (seen.size > 8192) seen.delete(seen.keys().next().value);
    queue.push(payload);
    if (!draining) drainPromise = drain().catch(() => { if (!stopped) console.warn("[AI Observatory] Structural event export failed; events may be missing."); });
  };
  const drain = async () => {
    if (draining || stopped) return;
    draining = true;
    try {
      while (!stopped && queue.length) {
        const payload = queue.shift();
        queueFullReported = false;
        let ok = false;
        try { ok = await send(config, payload, (request) => { activeRequest = request; }); }
        catch { /* Failed sends must not prevent the remaining queue from draining. */ }
        finally { activeRequest = undefined; }
        if (!ok && !stopped) console.warn("[AI Observatory] Structural event export failed; events may be missing.");
      }
    } finally { draining = false; }
  };
  const pause = () => new Promise((resolve) => {
    const done = () => { clearTimeout(timer); abort.signal.removeEventListener("abort", done); resolve(); };
    const timer = setTimeout(done, 1000);
    abort.signal.addEventListener("abort", done, { once: true });
    if (abort.signal.aborted) done();
  });
  const events = (async () => {
    while (!closing && !stopped) {
      try {
        for await (const event of ctx.event.subscribe({ signal: abort.signal, types })) {
          try {
            const payload = encodeEvent(event);
            if (payload && await belongs(ctx, event)) enqueue(event, payload);
          } catch { console.warn("[AI Observatory] A structural event could not be recorded."); }
        }
      } catch { /* The stream is retried after a bounded pause. */ }
      if (!closing && !stopped) {
        console.warn("[AI Observatory] Structural event stream disconnected; events may be missing.");
        await pause();
      }
    }
  })();
  const boundedWait = (promise, milliseconds) => new Promise((resolve) => {
    const timer = setTimeout(resolve, milliseconds);
    void promise.then(() => { clearTimeout(timer); resolve(); }, () => { clearTimeout(timer); resolve(); });
  });
  return async () => {
    closing = true;
    abort.abort();
    await boundedWait(events, 1000);
    await boundedWait(drainPromise, 5000);
    if (queue.length || activeRequest) console.warn("[AI Observatory] Structural event shutdown could not flush all events.");
    stopped = true;
    queue.length = 0;
    activeRequest?.destroy();
  };
}

export default { id: "ai-observatory.structural-events", setup: activate };
