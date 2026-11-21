import assert from "node:assert/strict";
import { createServer } from "node:http";
import { setTimeout as delay } from "node:timers/promises";
import { inspect } from "node:util";
import { gunzipSync } from "node:zlib";
import test from "node:test";
import { activate } from "../index.js";

function context(directory, options = {}) {
  const hooks = {};
  const queue = [];
  let wake = () => {};
  const register = async (name, callback) => {
    hooks[name] = callback;
    return { dispose: async () => { delete hooks[name]; } };
  };
  async function* subscribe({ signal }) {
    signal.addEventListener("abort", () => wake(), { once: true });
    while (!signal.aborted) {
      if (!queue.length) await new Promise((resolve) => { wake = resolve; });
      while (queue.length) yield queue.shift();
    }
  }
  return {
    ctx: {
      app: { version: "2.2.5" },
      location: { directory },
      options,
      session: { hook: register, get: async () => ({ location: { directory } }) },
      tool: { hook: register },
      storage: { get: async () => undefined, set: async () => {}, scan: async () => ({ entries: [] }), remove: async () => {} },
      event: { subscribe },
    },
    hooks,
    push(type, data) {
      queue.push({ id: `${type}-${data.sessionID}`, type, data, created: Date.now() });
      wake();
    },
  };
}

function exportedSpans(requests) {
  return requests.flatMap((request) => {
    const payload = JSON.parse(request);
    return (payload.resourceSpans ?? []).flatMap((resource) =>
      (resource.scopeSpans ?? []).flatMap((scope) => scope.spans ?? []),
    );
  });
}

function spanAttributes(span) {
  return Object.fromEntries((span.attributes ?? []).map(({ key, value }) => [
    key,
    value.stringValue ?? value.intValue ?? value.doubleValue ?? value.boolValue,
  ]));
}

async function until(predicate, timeout = 10000) {
  const end = Date.now() + timeout;
  while (Date.now() < end) {
    if (predicate()) return;
    await delay(25);
  }
  assert.fail("Timed out waiting for synthetic trace export");
}

test("real instrumentation isolates diagnostics, settings and exporter failures", async (t) => {
  const environment = ["OTEL_TRACES_SAMPLER", "OTEL_BSP_MAX_EXPORT_BATCH_SIZE", "OTEL_SPAN_ATTRIBUTE_VALUE_LENGTH_LIMIT", "LANGFUSE_DEBUG"];
  const before = new Map(environment.map((key) => [key, process.env[key]]));
  const diagnosticKey = Symbol.for("opentelemetry.js.api.1");
  const previousDiagnostic = globalThis[diagnosticKey];
  const diagnostics = [];
  const logger = Object.fromEntries(["debug", "info", "warn", "error", "verbose"].map((name) => [name, (...args) => diagnostics.push(inspect(args, { depth: 8 }))]));
  globalThis[diagnosticKey] = { version: "1.9.1", diag: logger };
  t.after(() => {
    for (const [key, value] of before) {
      if (value === undefined) delete process.env[key];
      else process.env[key] = value;
    }
    if (previousDiagnostic === undefined) delete globalThis[diagnosticKey];
    else globalThis[diagnosticKey] = previousDiagnostic;
  });
  process.env.OTEL_TRACES_SAMPLER = "always_off";
  process.env.OTEL_BSP_MAX_EXPORT_BATCH_SIZE = "0";
  process.env.OTEL_SPAN_ATTRIBUTE_VALUE_LENGTH_LIMIT = "1";
  process.env.LANGFUSE_DEBUG = "true";

  const requests = [];
  let mode = "fail";
  const server = createServer((req, res) => {
    const chunks = [];
    req.on("data", (chunk) => chunks.push(chunk));
    req.on("end", () => {
      const raw = Buffer.concat(chunks);
      const text = (req.headers["content-encoding"] === "gzip" ? gunzipSync(raw) : raw).toString("utf8");
      requests.push(text);
      res.writeHead(mode === "fail" ? 500 : 200, { "Content-Type": "application/json" });
      res.end(mode === "fail" ? "SYNTHETIC_PRIVATE_FIRST" : mode === "invalid" ? "SYNTHETIC_PRIVATE_ECHO" : "{}");
    });
  });
  await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
  t.after(() => new Promise((resolve) => server.close(resolve)));
  const config = { publicKey: "pk-lf-test", secretKey: "sk-lf-test", baseUrl: `http://127.0.0.1:${server.address().port}` };
  const first = context("/synthetic/first", { tags: ["first"] });
  const second = context("/synthetic/second", { tags: ["different"] });
  const cleanup = await activate(first.ctx, { readConfig: async () => config });
  const cleanupSecond = await activate(second.ctx, { readConfig: async () => config });
  assert.equal(typeof cleanup, "function");
  assert.equal(typeof cleanupSecond, "function");

  async function turn(sessionID, marker) {
    await first.hooks.prompt({ sessionID, messageID: `u-${sessionID}`, prompt: { text: marker } });
    first.push("session.step.started", { sessionID, assistantMessageID: `a-${sessionID}`, agent: "build", model: { id: "mock", providerID: "mock" } });
    first.push("session.text.ended", { sessionID, assistantMessageID: `a-${sessionID}`, text: "synthetic response" });
    first.push("session.step.ended", { sessionID, assistantMessageID: `a-${sessionID}`, cost: 0, tokens: { input: 1, output: 1, reasoning: 0, cache: { read: 0, write: 0 } }, finish: "stop" });
    first.push("session.execution.succeeded", { sessionID });
  }
  await turn("s1", "SYNTHETIC_PRIVATE_FIRST");
  await until(() => requests.length >= 1);
  assert.ok(requests[0].includes("SYNTHETIC_PRIVATE_FIRST"), "inherited sampler or limits disabled the trace");
  mode = "ok";
  await turn("s2", "SYNTHETIC_PRIVATE_SECOND");
  await until(() => requests.some((body) => body.includes("SYNTHETIC_PRIVATE_SECOND")));
  mode = "invalid";
  await turn("s3", "SYNTHETIC_PRIVATE_ECHO");
  await until(() => requests.some((body) => body.includes("SYNTHETIC_PRIVATE_ECHO")));
  await delay(100);
  mode = "ok";
  assert.ok(!diagnostics.join("\n").includes("SYNTHETIC_PRIVATE"), "global diagnostics received private span content");

  first.ctx.storage.get = async (key) => key.endsWith("/event-only/turn")
    ? JSON.stringify({ turn: { traceId: "11111111111111111111111111111111", spanId: "2222222222222222", traceFlags: 1 } })
    : undefined;
  first.push("session.step.started", { sessionID: "event-only", assistantMessageID: "event-only-a", agent: "build", model: { id: "mock", providerID: "mock" } });
  await until(() => globalThis.aiObservatoryTraceOwners.has("event-only"));
  await first.hooks.prompt({ sessionID: "unfinished", messageID: "unfinished-u", prompt: { text: "SYNTHETIC_UNFINISHED" } });
  assert.ok(globalThis.aiObservatoryTraceOwners.has("unfinished"));
  await cleanup();
  await cleanupSecond();
  await until(() => !globalThis.aiObservatoryTraceOwners.has("unfinished"), 8000);
  assert.equal(globalThis.aiObservatoryTraceOwners.has("event-only"), false);
});

test("real instrumentation rolls back partial hook registration", async () => {
  const registrations = new Set();
  let calls = 0;
  const state = context("/synthetic/broken");
  state.ctx.session.hook = async () => {
    if (++calls === 2) throw new Error("private failure detail");
    const registration = { dispose: async () => registrations.delete(registration) };
    registrations.add(registration);
    return registration;
  };
  const config = { publicKey: "pk-lf-test", secretKey: "sk-lf-test", baseUrl: "http://127.0.0.1:3001" };
  // Reuse the existing client settings so the failure happens at hook registration.
  const existing = globalThis.aiObservatoryLangfuseRuntimeState;
  delete globalThis.aiObservatoryLangfuseRuntimeState;
  try {
    assert.equal(await activate(state.ctx, { readConfig: async () => config }), undefined);
    assert.equal(registrations.size, 0);
  } finally {
    globalThis.aiObservatoryLangfuseRuntimeState = existing;
  }
});

test("reported zero and missing usage/cost stay distinct in normal and reload recovery", async (t) => {
  const requests = [];
  const server = createServer((req, res) => {
    const chunks = [];
    req.on("data", (chunk) => chunks.push(chunk));
    req.on("end", () => {
      const raw = Buffer.concat(chunks);
      requests.push((req.headers["content-encoding"] === "gzip" ? gunzipSync(raw) : raw).toString("utf8"));
      res.writeHead(200, { "Content-Type": "application/json" });
      res.end("{}");
    });
  });
  await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
  const previousClient = globalThis.aiObservatoryLangfuseRuntimeState;
  delete globalThis.aiObservatoryLangfuseRuntimeState;
  let cleanup;
  t.after(async () => {
    try {
      await cleanup?.();
    } finally {
      if (previousClient === undefined) delete globalThis.aiObservatoryLangfuseRuntimeState;
      else globalThis.aiObservatoryLangfuseRuntimeState = previousClient;
      await new Promise((resolve) => server.close(resolve));
    }
  });
  const state = context("/synthetic/numeric-provenance");
  const messages = new Map();
  state.ctx.session.context = async ({ sessionID }) => messages.get(sessionID) ?? [];
  cleanup = await activate(state.ctx, {
    readConfig: async () => ({
      publicKey: "pk-lf-test",
      secretKey: "sk-lf-test",
      baseUrl: `http://127.0.0.1:${server.address().port}`,
    }),
  });
  assert.equal(typeof cleanup, "function");

  const model = { id: "synthetic-model", providerID: "synthetic-provider" };
  async function promptAndStart(sessionID, messageID) {
    await state.hooks.prompt({ sessionID, messageID: `u-${messageID}`, prompt: { text: "synthetic" } });
    state.push("session.step.started", {
      sessionID,
      assistantMessageID: messageID,
      agent: "build",
      model,
    });
    state.push("session.text.ended", { sessionID, assistantMessageID: messageID, text: "synthetic response" });
  }
  function generation(messageID) {
    return exportedSpans(requests).find((span) => {
      if (span.name !== "opencode.generation") return false;
      const attributes = spanAttributes(span);
      try {
        return JSON.parse(attributes["langfuse.observation.metadata"]).messageID === messageID;
      } catch {
        return false;
      }
    });
  }
  async function waitForGeneration(messageID) {
    await until(() => generation(messageID));
    return spanAttributes(generation(messageID));
  }

  await promptAndStart("normal-missing", "normal-missing-message");
  state.push("session.step.ended", {
    sessionID: "normal-missing",
    assistantMessageID: "normal-missing-message",
    finish: "stop",
  });
  state.push("session.execution.succeeded", { sessionID: "normal-missing" });
  const normalMissing = await waitForGeneration("normal-missing-message");
  assert.equal(normalMissing["langfuse.observation.usage_details"], undefined);
  assert.equal(normalMissing["langfuse.observation.cost_details"], undefined);

  await promptAndStart("normal-zero", "normal-zero-message");
  state.push("session.step.ended", {
    sessionID: "normal-zero",
    assistantMessageID: "normal-zero-message",
    cost: 0,
    tokens: { input: 0 },
    finish: "stop",
  });
  state.push("session.execution.succeeded", { sessionID: "normal-zero" });
  const normalZero = await waitForGeneration("normal-zero-message");
  assert.deepEqual(JSON.parse(normalZero["langfuse.observation.usage_details"]), { input: 0 });
  assert.deepEqual(JSON.parse(normalZero["langfuse.observation.cost_details"]), { total: 0 });

  async function recover(sessionID, messageID, numericFields) {
    messages.set(sessionID, [{
      type: "assistant",
      id: messageID,
      content: [{ type: "text", text: "synthetic response" }],
      finish: "stop",
      ...numericFields,
    }]);
    await promptAndStart(sessionID, messageID);
    state.push("session.execution.succeeded", { sessionID });
    return waitForGeneration(messageID);
  }

  const recoveredMissing = await recover("recovery-missing", "recovery-missing-message", {});
  assert.equal(recoveredMissing["langfuse.observation.usage_details"], undefined);
  assert.equal(recoveredMissing["langfuse.observation.cost_details"], undefined);

  const recoveredZero = await recover("recovery-zero", "recovery-zero-message", {
    cost: 0,
    tokens: { input: 0, output: 0, reasoning: 0, cache: { read: 0, write: 0 }, total: 0 },
  });
  assert.deepEqual(JSON.parse(recoveredZero["langfuse.observation.usage_details"]), {
    input: 0, output: 0, reasoning: 0, cache_read: 0, cache_write: 0, total: 0,
  });
  assert.deepEqual(JSON.parse(recoveredZero["langfuse.observation.cost_details"]), { total: 0 });
  await cleanup();
  cleanup = undefined;
});
