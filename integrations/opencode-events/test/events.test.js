import assert from "node:assert/strict";
import { spawn, spawnSync } from "node:child_process";
import { mkdtemp, readFile, rm } from "node:fs/promises";
import { createServer } from "node:http";
import { createServer as createHttpsServer } from "node:https";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { setTimeout as delay } from "node:timers/promises";
import test from "node:test";
import { activate, encodeEvent, validateConfig } from "../index.js";

const seenKey = Symbol.for("ai-observatory.structural-events.seen.v1");
const base = { id: "evt_synthetic", created: 1795100000000, type: "session.execution.succeeded",
  data: { sessionID: "ses_synthetic", error: { message: "SYNTHETIC_SECRET" }, text: "SYNTHETIC_SECRET" },
  location: { directory: "/synthetic/project" } };

function context(directory = "/synthetic/project") {
  const queue = [];
  let wake = () => {};
  return {
    ctx: {
      location: { directory },
      session: { get: async () => ({ location: { directory } }) },
      event: { async *subscribe({ signal }) {
        signal.addEventListener("abort", () => wake(), { once: true });
        while (!signal.aborted) {
          if (!queue.length) await new Promise((resolve) => { wake = resolve; });
          while (queue.length) yield queue.shift();
        }
      } },
    },
    push(event) { queue.push(event); wake(); },
  };
}

async function until(predicate) {
  for (let count = 0; count < 200; count++) {
    if (predicate()) return;
    await delay(20);
  }
  assert.fail("Synthetic event was not exported in time");
}

test("only allowed structural fields survive encoding", () => {
  const payload = encodeEvent(base);
  assert.ok(payload);
  assert.doesNotMatch(JSON.stringify(payload), /SYNTHETIC_SECRET/);
  const span = payload.resourceSpans[0].scopeSpans[0].spans[0];
  assert.equal(span.name, "aiobs.execution.succeeded");
  assert.equal(span.traceId.length, 32);
  assert.equal(span.spanId.length, 16);
  assert.deepEqual(encodeEvent(base), payload);
  assert.equal(encodeEvent({ ...base, type: "session.text.ended" }), null);
  assert.equal(encodeEvent({ ...base, id: "invalid/id" }), null);
  assert.equal(encodeEvent({ ...base, data: { sessionID: "invalid/id" } }), null);
  const compact = encodeEvent({ ...base, type: "session.compaction.ended", data: { ...base.data, reason: "auto",
    tokens: { input: 12, output: 3, reasoning: -1, cache: { read: 4, write: "SYNTHETIC_SECRET" } } } });
  assert.equal(compact.resourceSpans[0].scopeSpans[0].spans[0].name, "aiobs.compaction.ended.auto");
  const attrs = compact.resourceSpans[0].scopeSpans[0].spans[0].attributes;
  const usage = JSON.parse(attrs.find((item) => item.key === "langfuse.observation.usage_details").value.stringValue);
  assert.deepEqual(usage, { input: 12, output: 3, cache_read: 4 });
  assert.doesNotMatch(JSON.stringify(compact), /SYNTHETIC_SECRET/);
  const absent = encodeEvent({ ...base, type: "session.compaction.failed", data: { ...base.data, tokens: { input: NaN, output: 1.5, reasoning: -1 } } });
  assert.equal(absent.resourceSpans[0].scopeSpans[0].spans[0].attributes.some((item) => item.key === "langfuse.observation.usage_details"), false);
  const execution = encodeEvent({ ...base, data: { ...base.data, tokens: { input: 100 } } });
  assert.equal(execution.resourceSpans[0].scopeSpans[0].spans[0].attributes.some((item) => item.key === "langfuse.observation.usage_details"), false);
});

test("private destinations require exact loopback or HTTPS tailnet origin", () => {
  const c = { publicKey: "pk-lf-synthetic", secretKey: "sk-lf-synthetic", baseUrl: "http://127.0.0.1:3001" };
  assert.equal(validateConfig(c), true);
  for (const baseUrl of ["http://localhost:3001", "https://example.com:443", "http://127.0.0.1:3001/path", "http://a@127.0.0.1:3001"]) {
    assert.equal(validateConfig({ ...c, baseUrl }), false);
  }
});

test("event subscription filters location and deduplicates across plugin instances", async (t) => {
  delete globalThis[seenKey];
  const requests = [];
  const server = createServer((request, response) => {
    const chunks = [];
    request.on("data", (chunk) => chunks.push(chunk));
    request.on("end", () => { requests.push({ headers: request.headers, body: Buffer.concat(chunks).toString() }); response.writeHead(200, { "Content-Type": "application/json" }); response.end("{}"); });
  });
  await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
  t.after(() => new Promise((resolve) => server.close(resolve)));
  const config = { publicKey: "pk-lf-synthetic", secretKey: "sk-lf-synthetic", baseUrl: `http://127.0.0.1:${server.address().port}` };
  const first = context();
  const second = context();
  const firstStop = await activate(first.ctx, { readConfig: async () => config });
  const secondStop = await activate(second.ctx, { readConfig: async () => config });
  first.push({ ...base, id: "evt_wrong_location", location: { directory: "/other" } });
  first.push(base);
  second.push(base);
  await until(() => requests.length === 1);
  await delay(100);
  assert.equal(requests.length, 1);
  assert.doesNotMatch(requests[0].body, /SYNTHETIC_SECRET|evt_wrong_location/);
  assert.match(requests[0].headers.authorization, /^Basic /);
  await firstStop();
  await secondStop();
});

test("cleanup allows an in-flight event to flush", async (t) => {
  delete globalThis[seenKey];
  let requests = 0;
  let responses = 0;
  const server = createServer((request, response) => {
    request.resume();
    request.on("end", () => {
      requests++;
      setTimeout(() => { responses++; response.writeHead(200); response.end("{}"); }, 75);
    });
  });
  await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
  t.after(() => new Promise((resolve) => server.close(resolve)));
  const config = { publicKey: "pk-lf-synthetic", secretKey: "sk-lf-synthetic", baseUrl: `http://127.0.0.1:${server.address().port}` };
  const state = context();
  const stop = await activate(state.ctx, { readConfig: async () => config });
  state.push(base);
  await until(() => requests === 1);
  await stop();
  assert.equal(responses, 1);
});

test("a full queue drops excess events with one generic warning until capacity returns", async (t) => {
  delete globalThis[seenKey];
  let requests = 0;
  let release;
  const server = createServer((request, response) => {
    request.resume();
    request.on("end", () => {
      requests++;
      if (requests === 1) release = () => { response.writeHead(200); response.end("{}"); };
      else { response.writeHead(200); response.end("{}"); }
    });
  });
  await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
  t.after(() => new Promise((resolve) => server.close(resolve)));
  const config = { publicKey: "pk-lf-synthetic", secretKey: "sk-lf-synthetic", baseUrl: `http://127.0.0.1:${server.address().port}` };
  const oldWarn = console.warn;
  const warnings = [];
  console.warn = (...args) => warnings.push(args.join(" "));
  let stop;
  try {
    const state = context();
    stop = await activate(state.ctx, { readConfig: async () => config });
    for (let i = 0; i < 160; i++) state.push({ ...base, id: `evt_queue_${i}` });
    await until(() => release && warnings.length > 0);
    await delay(50);
    assert.equal(warnings.length, 1);
    assert.match(warnings[0], /queue is full/);
    assert.doesNotMatch(warnings[0], /SYNTHETIC_SECRET|evt_queue/);
    release();
    release = undefined;
    await until(() => requests === 129);
    await stop();
    stop = undefined;
  } finally {
    if (release && requests === 1) release();
    if (stop) await stop();
    console.warn = oldWarn;
  }
});

test("global or event-only opt-out prevents subscription", async () => {
  const old = process.env.AI_OBSERVATORY_EVENTS_DISABLED;
  process.env.AI_OBSERVATORY_EVENTS_DISABLED = "1";
  try {
    const state = context();
    assert.equal(await activate(state.ctx, { readConfig: async () => { throw new Error("must not read"); } }), undefined);
  } finally {
    if (old === undefined) delete process.env.AI_OBSERVATORY_EVENTS_DISABLED;
    else process.env.AI_OBSERVATORY_EVENTS_DISABLED = old;
  }
});

test("configuration errors are contained without logging private details", async () => {
  const oldWarn = console.warn;
  const warnings = [];
  console.warn = (...args) => warnings.push(args.join(" "));
  try {
    const state = context();
    assert.equal(await activate(state.ctx, { readConfig: async () => { throw new Error("SYNTHETIC_PRIVATE_CONFIG"); } }), undefined);
    assert.ok(warnings.length);
    assert.doesNotMatch(warnings.join(" "), /SYNTHETIC_PRIVATE_CONFIG/);
  } finally { console.warn = oldWarn; }
});

test("redirects are not followed and failures never log response bodies", async (t) => {
  delete globalThis[seenKey];
  let decoyRequests = 0;
  const decoy = createServer((_request, response) => { decoyRequests++; response.end("{}"); });
  await new Promise((resolve) => decoy.listen(0, "127.0.0.1", resolve));
  const first = createServer((_request, response) => {
    response.writeHead(302, { Location: `http://127.0.0.1:${decoy.address().port}/leak` });
    response.end("SYNTHETIC_SECRET_RESPONSE");
  });
  await new Promise((resolve) => first.listen(0, "127.0.0.1", resolve));
  t.after(() => Promise.all([first, decoy].map((server) => new Promise((resolve) => server.close(resolve)))));
  const config = { publicKey: "pk-lf-synthetic", secretKey: "sk-lf-synthetic", baseUrl: `http://127.0.0.1:${first.address().port}` };
  const warnings = [];
  const oldWarn = console.warn;
  console.warn = (...args) => warnings.push(args.join(" "));
  try {
    const state = context();
    const stop = await activate(state.ctx, { readConfig: async () => config });
    state.push(base);
    await until(() => warnings.length > 0);
    await stop();
    assert.equal(decoyRequests, 0);
    assert.doesNotMatch(warnings.join(" "), /SYNTHETIC_SECRET|sk-lf-synthetic/);
  } finally { console.warn = oldWarn; }
});

test("inherited proxy settings cannot divert structural events", async (t) => {
  let targetRequests = 0;
  let decoyRequests = 0;
  const target = createServer((request, response) => { request.resume(); request.on("end", () => { targetRequests++; response.writeHead(200); response.end("{}"); }); });
  const decoy = createServer((_request, response) => { decoyRequests++; response.writeHead(500); response.end(); });
  await Promise.all([target, decoy].map((server) => new Promise((resolve) => server.listen(0, "127.0.0.1", resolve))));
  t.after(() => Promise.all([target, decoy].map((server) => new Promise((resolve) => server.close(resolve)))));
  const moduleUrl = new URL("../index.js", import.meta.url).href;
  const config = { publicKey: "pk-lf-synthetic", secretKey: "sk-lf-synthetic", baseUrl: `http://127.0.0.1:${target.address().port}` };
  const program = `
    const { activate } = await import(${JSON.stringify(moduleUrl)});
    let emitted = false;
    const ctx = { location: { directory: "/synthetic/project" }, session: { get: async () => ({}) },
      event: { async *subscribe({ signal }) {
        if (!emitted) { emitted = true; yield ${JSON.stringify(base)}; }
        await new Promise((resolve) => signal.addEventListener("abort", resolve, { once: true }));
      } } };
    const stop = await activate(ctx, { readConfig: async () => (${JSON.stringify(config)}) });
    await new Promise((resolve) => setTimeout(resolve, 300));
    await stop();
  `;
  const proxy = `http://127.0.0.1:${decoy.address().port}`;
  const child = spawn(process.execPath, ["--input-type=module", "-e", program], {
    env: { ...process.env, AI_OBSERVATORY_DISABLED: "0", AI_OBSERVATORY_EVENTS_DISABLED: "0", NODE_DEBUG: "", NODE_DEBUG_NATIVE: "", HTTP_PROXY: proxy, HTTPS_PROXY: proxy, ALL_PROXY: proxy, NO_PROXY: "", NODE_USE_ENV_PROXY: "1" },
    stdio: ["ignore", "ignore", "pipe"],
  });
  let stderr = "";
  child.stderr.on("data", (chunk) => { stderr += chunk; });
  const code = await new Promise((resolve) => child.on("exit", resolve));
  assert.equal(code, 0, stderr);
  assert.equal(targetRequests, 1);
  assert.equal(decoyRequests, 0);
});

test("inherited insecure TLS setting does not disable certificate verification", async (t) => {
  if (spawnSync("openssl", ["version"], { stdio: "ignore" }).status !== 0) return t.skip("openssl unavailable");
  const root = await mkdtemp(join(tmpdir(), "aiobs-events-tls-"));
  t.after(() => rm(root, { recursive: true, force: true }));
  const key = join(root, "synthetic.key");
  const cert = join(root, "synthetic.crt");
  const generated = spawnSync("openssl", ["req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", key,
    "-out", cert, "-days", "1", "-subj", "/CN=synthetic.ts.net"], { stdio: "ignore" });
  assert.equal(generated.status, 0);
  let requests = 0;
  let handshakeErrors = 0;
  const target = createHttpsServer({ key: await readFile(key), cert: await readFile(cert) }, (_request, response) => { requests++; response.end("{}"); });
  target.on("tlsClientError", () => { handshakeErrors++; });
  await new Promise((resolve) => target.listen(0, "127.0.0.1", resolve));
  t.after(() => new Promise((resolve) => target.close(resolve)));
  const moduleUrl = new URL("../index.js", import.meta.url).href;
  const config = { publicKey: "pk-lf-synthetic", secretKey: "sk-lf-synthetic", baseUrl: `https://synthetic.ts.net:${target.address().port}` };
  const program = `
    import dns from "node:dns";
    dns.lookup = (_host, options, callback) => {
      const cb = typeof options === "function" ? options : callback;
      if (options && options.all) cb(null, [{ address: "127.0.0.1", family: 4 }]);
      else cb(null, "127.0.0.1", 4);
    };
    const { activate } = await import(${JSON.stringify(moduleUrl)});
    let emitted = false;
    const ctx = { location: { directory: "/synthetic/project" }, session: { get: async () => ({}) },
      event: { async *subscribe({ signal }) {
        if (!emitted) { emitted = true; yield ${JSON.stringify(base)}; }
        await new Promise((resolve) => signal.addEventListener("abort", resolve, { once: true }));
      } } };
    const stop = await activate(ctx, { readConfig: async () => (${JSON.stringify(config)}) });
    await new Promise((resolve) => setTimeout(resolve, 300));
    await stop();
  `;
  const child = spawn(process.execPath, ["--input-type=module", "-e", program], {
    env: { ...process.env, AI_OBSERVATORY_DISABLED: "0", AI_OBSERVATORY_EVENTS_DISABLED: "0", NODE_DEBUG: "", NODE_DEBUG_NATIVE: "", NODE_TLS_REJECT_UNAUTHORIZED: "0", NODE_USE_ENV_PROXY: "0" },
    stdio: "ignore",
  });
  const code = await new Promise((resolve) => child.on("exit", resolve));
  assert.equal(code, 0);
  assert.equal(requests, 0);
  assert.ok(handshakeErrors > 0, "synthetic TLS handshake was not attempted");
});
