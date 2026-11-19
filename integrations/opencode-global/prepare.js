/** Apply narrowly scoped, integrity-checked changes to the pinned upstream bundle. */
import { createHash } from "node:crypto";
import { lstat, readFile, writeFile } from "node:fs/promises";
import { fileURLToPath } from "node:url";

const runtime = new URL("./node_modules/@langfuse/opencode-observability-plugin/dist/runtime-ku15Ukfq.js", import.meta.url);
const v2 = new URL("./node_modules/@langfuse/opencode-observability-plugin/dist/v2/index.js", import.meta.url);
export const pluginUrl = v2;
const packageMetadata = new URL("./node_modules/@langfuse/opencode-observability-plugin/package.json", import.meta.url);
const directories = [
  "./node_modules/",
  "./node_modules/@langfuse/",
  "./node_modules/@langfuse/opencode-observability-plugin/",
  "./node_modules/@langfuse/opencode-observability-plugin/dist/",
  "./node_modules/@langfuse/opencode-observability-plugin/dist/v2/",
].map((path) => new URL(path, import.meta.url));
const packageHash = "95e1aed223553408e0e5e7d5f88607adbbcabe9d14d51e9a3803d869379ee33e";
const originalHashes = [
  "7bc4e2683c22c5daa4cb7a6b37210b983ef6dff6dd0de868d1894e9285cbb752",
  "61b3bb76f11ff455fbc8341a18e80545668a9c7fc47108b79e7543508a87a45d",
];
// Prepared hashes are checked again during activation: an unpatched or unexpected
// upstream dependency must never silently read unrelated credentials or use Cloud.
const preparedHashes = [
  "1191b6652ee103d3d7cf40872e269b999a5c0e6ab4116093e34f0775ab059d69",
  "10ca52af741def9e059af38b9dfe98260c1c9a6e91802caa91dbb9264f6269a4",
];
const digest = (buffer) => createHash("sha256").update(buffer).digest("hex");

function replaceOnce(source, from, to) {
  if (source.split(from).length !== 2) throw new Error("Unexpected pinned upstream bundle");
  return source.replace(from, to);
}

function replaceCount(source, from, to, expected) {
  if (source.split(from).length !== expected + 1) throw new Error("Unexpected pinned upstream bundle");
  return source.replaceAll(from, to);
}

function patchRuntime(source) {
  // The bundled Langfuse SDK otherwise reads logging, release, batch and
  // credential settings from unrelated process-wide environment variables.
  source = replaceOnce(source,
    'function getEnv(key) {\n\tif (typeof process !== "undefined" && process.env[key]) return process.env[key];\n\telse if (typeof globalThis !== "undefined") return globalThis[key];\n}',
    "function getEnv(_key) { return void 0; }",
  );
  // Keep inherited sampling, limits and batch settings from silently changing
  // this private tracer. These helpers are local to our bundled copy.
  source = replaceCount(source, "const raw = process.env[key];", "const raw = void 0;", 2);
  source = replaceOnce(source, "const raw = process.env[key]?.trim().toLowerCase();", "const raw = void 0;");
  // Another plugin can install a process-wide OpenTelemetry diagnostic logger.
  // Do not forward this bundled copy's diagnostics to that global logger.
  source = replaceOnce(source, "function getGlobal(type) {\n\tvar _a, _b;", "function getGlobal(type) {\n\tif (type === \"diag\") return;\n\tvar _a, _b;");
  source = replaceOnce(source, 'this._diagLogger.debug("items to be sent", internalRepresentation);', "");
  source = replaceOnce(source,
    'this._diagLogger.warn("Export succeeded but could not deserialize response - is the response specification compliant?", e, response.data);',
    'this._diagLogger.warn("Export succeeded but the response could not be decoded.");',
  );
  source = replaceOnce(source,
    'diag.warn("Received Partial Success response:", JSON.stringify(response.partialSuccess));',
    'diag.warn("The trace endpoint reported a partial export.");',
  );
  // Do not merge inherited OTEL headers or TLS certificates into this exporter.
  source = replaceOnce(source,
    "}, getNodeHttpConfigurationFromEnvironment(signalIdentifier, signalResourcePath), getNodeHttpConfigurationDefaults(requiredHeaders, signalResourcePath));",
    "}, {}, getNodeHttpConfigurationDefaults(requiredHeaders, signalResourcePath));",
  );
  // A process-wide NODE_TLS_REJECT_UNAUTHORIZED=0 must not weaken this exporter.
  source = replaceOnce(source, "return new Agent(options);", "return new Agent({ ...options, rejectUnauthorized: true });");
  source = replaceOnce(source,
    "resource: defaultResource().merge(detectResources({ detectors: [envDetector] })).merge(input.serviceName != null ? resourceFromAttributes({ \"service.name\": input.serviceName }) : null),",
    "resource: defaultResource().merge(input.serviceName != null ? resourceFromAttributes({ \"service.name\": input.serviceName }) : null),",
  );
  source = replaceOnce(source,
    "const credentials = yield* loadLangfuseCredentials;",
    'const credentials = input.credentials;\n\tif (!credentials?.publicKey || !credentials?.secretKey || !credentials?.baseUrl) return yield* fail$1(new MissingLangfuseCredentials({ message: "Missing private AI Observatory credentials" }));',
  );
  source = replaceOnce(source,
    'baseUrl: credentials.baseUrl ?? process.env.LANGFUSE_BASE_URL ?? process.env.LANGFUSE_BASEURL ?? "https://cloud.langfuse.com",\n\t\tenvironment: credentials.environment ?? process.env.LANGFUSE_ENVIRONMENT ?? "development",\n\t\tuserId: credentials.userId ?? process.env.LANGFUSE_USER_ID,\n\t\tserviceName: credentials.serviceName ?? process.env.LANGFUSE_SERVICE_NAME,',
    'baseUrl: credentials.baseUrl,\n\t\tenvironment: credentials.environment ?? "development",\n\t\tuserId: credentials.userId,\n\t\tserviceName: credentials.serviceName,',
  );
  source = replaceOnce(source,
    'const sharedClientState = globalThis.langfuseOpencodeRuntimeState ?? runSync$1(make({ _tag: "Empty" }));\n\tglobalThis.langfuseOpencodeRuntimeState = sharedClientState;',
    'const sharedClientState = globalThis.aiObservatoryLangfuseRuntimeState ?? runSync$1(make({ _tag: "Empty" }));\n\tglobalThis.aiObservatoryLangfuseRuntimeState = sharedClientState;',
  );
  return replaceOnce(source,
    "\t\tbaseUrl: input.baseUrl,\n\t\tenvironment: input.environment,\n\t\tshouldExportSpan:",
    "\t\tbaseUrl: input.baseUrl,\n\t\tenvironment: input.environment,\n\t\tmediaUploadEnabled: false,\n\t\tcompression: \"gzip\",\n\t\tshouldExportSpan:",
  );
}

function patchV2(source) {
  source = replaceOnce(source,
    "\t\t\topencodeVersion: ctx.app.version,\n\t\t\ttags\n\t\t}).pipe",
    "\t\t\topencodeVersion: ctx.app.version,\n\t\t\ttags,\n\t\t\tcredentials: ctx.options.__aiObservatoryCredentials\n\t\t}).pipe",
  );
  source = replaceCount(source, "await runPromise(langfuse.forceFlush);", "await flush();", 3);
  source = replaceOnce(source,
    "\t\tconst registrations = [];\n\t\tconst userMessageIDs",
    `\t\tconst registrations = [];
\t\tconst locations = globalThis.aiObservatoryTraceLocations ??= new Map();
\t\tconst owners = globalThis.aiObservatoryTraceOwners ??= new Map();
\t\tconst locationID = JSON.stringify([ctx.location?.directory ?? "", ctx.location?.workspaceID ?? ""]);
\t\tconst tracked = globalThis.aiObservatoryTraceTracked ??= new Map();
\t\tconst location = locations.get(locationID) ?? { sessions: new Set(), timer: undefined, generation: undefined };
\t\tlocation.userMessageIDs ??= new Map();
\t\tlocation.resolvedSessionParents ??= new Set();
\t\tlocation.generationDetails ??= new Map();
\t\tconst previousGeneration = location.generation;
\t\tconst wasRetired = location.retired === true;
\t\tif (location.timer) clearTimeout(location.timer);
\t\tconst generation = Symbol();
\t\tlocation.generation = generation;
\t\tlocation.retired = false;
\t\tlocations.set(locationID, location);
\t\tconst rememberOwner = (sessionID) => {
\t\t\tif (typeof sessionID !== "string") return;
\t\t\tconst previous = owners.get(sessionID);
\t\t\tif (previous && previous !== location) previous.sessions.delete(sessionID);
\t\t\tlocation.sessions.add(sessionID);
\t\t\towners.set(sessionID, location);
\t\t};
\t\tconst rememberID = (sessionID, group, id) => {
\t\t\tif (typeof sessionID !== "string" || typeof id !== "string") return;
\t\t\tconst item = tracked.get(sessionID) ?? { messages: new Set(), events: new Set(), calls: new Set() };
\t\t\titem[group].add(id);
\t\t\ttracked.set(sessionID, item);
\t\t};
\t\tconst flush = async () => {
\t\t\ttry { await runPromise(langfuse.forceFlush); }
\t\t\tcatch { console.warn("[AI Observatory] Trace export failed; some observations may be lost."); }
\t\t};
\t\tconst clearSession = (sessionID) => {
\t\t\tlangfuse.endActiveToolObservations(sessionID);
\t\t\tlangfuse.endActiveGenerationSteps(sessionID);
\t\t\tlangfuse.endActiveTurnObservations(sessionID);
\t\t\tlangfuse.clearSessionTraceState(sessionID);
\t\t\tconst state = langfuse.traceState;
\t\t\tconst ids = tracked.get(sessionID);
\t\t\tif (ids) {
\t\t\t\tfor (const id of ids.messages) {
\t\t\t\t\tstate.tracedMessageIds.delete(id);
\t\t\t\t\tstate.tracedGenerationIds.delete(id);
\t\t\t\t\tstate.generationSpansByMessageId.delete(id);
\t\t\t\t\tstate.assistantParts.delete(id);
\t\t\t\t}
\t\t\t\tfor (const id of ids.events) state.tracedEventIds.delete(id);
\t\t\t\tfor (const id of ids.calls) {
\t\t\t\t\tstate.finalizedToolCallIds.delete(id);
\t\t\t\t\tstate.toolMessageIdsByCallId.delete(id);
\t\t\t\t}
\t\t\t}
\t\t\tstate.sessionParentIds.delete(sessionID);
\t\t\ttracked.delete(sessionID);
\t\t};
\t\tconst retireLocation = (targetGeneration = generation) => {
\t\t\tif (location.generation !== targetGeneration) return;
\t\t\tlocation.retired = true;
\t\t\tlocation.timer = setTimeout(() => {
\t\t\t\tif (location.generation !== targetGeneration) return;
\t\t\t\tfor (const sessionID of location.sessions) {
\t\t\t\t\tif (owners.get(sessionID) !== location) continue;
\t\t\t\t\ttry { clearSession(sessionID); }
\t\t\t\t\tcatch { console.warn("[AI Observatory] Could not clear a retired trace session."); }
\t\t\t\t\towners.delete(sessionID);
\t\t\t\t}
\t\t\t\tlocation.sessions.clear();
\t\t\t\tlocation.userMessageIDs.clear();
\t\t\t\tlocation.resolvedSessionParents.clear();
\t\t\t\tlocation.generationDetails.clear();
\t\t\t\tlocations.delete(locationID);
\t\t\t\tvoid flush();
\t\t\t}, 5000);
\t\t\tlocation.timer.unref?.();
\t\t};
\t\tconst userMessageIDs`,
  );
  source = replaceOnce(source,
    "const userMessageIDs = /* @__PURE__ */ new Map();\n\t\tconst resolvedSessionParents = /* @__PURE__ */ new Set();\n\t\tconst generationDetails = /* @__PURE__ */ new Map();",
    `const userMessageIDs = location.userMessageIDs;
\t\tconst resolvedSessionParents = location.resolvedSessionParents;
\t\tconst generationDetails = location.generationDetails;
\t\tconst recoverFinishedGenerations = async (sessionID, completed) => {
\t\t\tconst pending = [...generationDetails].filter(([, detail]) => detail.sessionID === sessionID);
\t\t\tif (!pending.length || typeof ctx.session.context !== "function") return;
\t\t\ttry {
\t\t\t\tconst messages = await ctx.session.context({ sessionID });
\t\t\t\tfor (const [messageID, detail] of pending) {
\t\t\t\t\tconst message = messages.find((item) => item.type === "assistant" && item.id === messageID);
\t\t\t\t\tif (!message || !Array.isArray(message.content)) {
\t\t\t\t\t\tconsole.warn("[AI Observatory] An in-flight generation could not be recovered after reload.");
\t\t\t\t\t\tcontinue;
\t\t\t\t\t}
\t\t\t\t\tconst tokens = message.tokens ?? { input: 0, output: 0, reasoning: 0, cache: { read: 0, write: 0 } };
\t\t\t\t\tlangfuse.traceGeneration({
\t\t\t\t\t\tsessionID, messageID, parentID: userMessageIDs.get(sessionID) ?? "",
\t\t\t\t\t\tmodelID: detail.model.id, providerID: detail.model.providerID,
\t\t\t\t\t\tagent: detail.agent, mode: detail.agent, created: detail.started,
\t\t\t\t\t\tcompleted: message.time?.completed ?? completed, finish: message.finish,
\t\t\t\t\t\tcost: message.cost ?? 0, tokens,
\t\t\t\t\t\toutput: [{ role: "assistant", content: message.content }],
\t\t\t\t\t});
\t\t\t\t\tgenerationDetails.delete(messageID);
\t\t\t\t}
\t\t\t} catch { console.warn("[AI Observatory] Could not recover an in-flight generation after reload."); }
\t\t};`,
  );
  for (const hook of ["prompt", "context"]) {
    const prefix = hook === "prompt" ? "async (input) => {" : "(input) => {";
    source = replaceOnce(source,
      `ctx.session.hook("${hook}", ${prefix}`,
      `ctx.session.hook("${hook}", ${prefix}\n\t\t\trememberOwner(input.sessionID);${hook === "prompt" ? '\n\t\t\trememberID(input.sessionID, "messages", input.messageID);' : ""}`,
    );
  }
  for (const hook of ["execute.before", "execute.after"]) {
    source = replaceOnce(source,
      `ctx.tool.hook("${hook}", async (input) => {`,
      `ctx.tool.hook("${hook}", async (input) => {\n\t\t\trememberOwner(input.sessionID);\n\t\t\trememberID(input.sessionID, "messages", input.messageID);\n\t\t\trememberID(input.sessionID, "calls", input.id);`,
    );
  }
  source = replaceOnce(source,
    "\t\tregistrations.push(await ctx.session.hook(\"prompt\"",
    "\t\ttry {\n\t\tregistrations.push(await ctx.session.hook(\"prompt\"",
  );
  source = replaceOnce(source,
    "\t\tconst events = (async () => {\n\t\t\ttry {\n\t\t\t\tfor await (const event of ctx.event.subscribe({ signal: abort.signal })) {",
    `\t\tconst pause = () => new Promise((resolve) => {
\t\t\tconst done = () => { clearTimeout(timer); abort.signal.removeEventListener("abort", done); resolve(); };
\t\t\tconst timer = setTimeout(done, 1000);
\t\t\tabort.signal.addEventListener("abort", done, { once: true });
\t\t\tif (abort.signal.aborted) done();
\t\t});
\t\tconst events = (async () => {
\t\t\twhile (!abort.signal.aborted) {
\t\t\ttry {
\t\t\t\tfor await (const event of ctx.event.subscribe({ signal: abort.signal })) {
\t\t\t\t\ttry {
\t\t\t\t\t\tconst sessionID = event.data?.sessionID;
\t\t\t\t\t\tif (typeof sessionID !== "string") continue;
\t\t\t\t\t\tif (event.location?.directory) {
\t\t\t\t\t\t\tif (event.location.directory !== ctx.location?.directory || (event.location.workspaceID !== undefined && event.location.workspaceID !== ctx.location?.workspaceID)) continue;
\t\t\t\t\t\t\trememberOwner(sessionID);
\t\t\t\t\t\t} else if (owners.has(sessionID)) {
\t\t\t\t\t\t\tif (owners.get(sessionID) !== location) continue;
\t\t\t\t\t\t} else {
\t\t\t\t\t\t\ttry {
\t\t\t\t\t\t\t\tconst session = await ctx.session.get({ sessionID });
\t\t\t\t\t\t\t\tif (!ctx.location?.directory || session.location?.directory !== ctx.location.directory) continue;
\t\t\t\t\t\t\t\trememberOwner(sessionID);
\t\t\t\t\t\t\t} catch { continue; }
\t\t\t\t\t\t}
\t\t\t\t\t\trememberID(sessionID, "messages", event.data.assistantMessageID);
\t\t\t\t\t\tif (event.type === "session.retry.scheduled") rememberID(sessionID, "events", event.id);`,
  );
  source = replaceOnce(source,
    "\t\t\t\t\tif (event.type === \"session.deleted\") {\n\t\t\t\t\t\tlangfuse.clearSessionTraceState(event.data.sessionID);",
    "\t\t\t\t\tif (event.type === \"session.deleted\") {\n\t\t\t\t\t\towners.get(event.data.sessionID)?.sessions.delete(event.data.sessionID);\n\t\t\t\t\t\towners.delete(event.data.sessionID);\n\t\t\t\t\t\tclearSession(event.data.sessionID);\n\t\t\t\t\t\tuserMessageIDs.delete(event.data.sessionID);\n\t\t\t\t\t\tfor (const [id, detail] of generationDetails) if (detail.sessionID === event.data.sessionID) generationDetails.delete(id);",
  );
  source = replaceOnce(source,
    'if (event.type === "session.execution.succeeded" || event.type === "session.execution.interrupted") {\n\t\t\t\t\t\tlangfuse.endActiveToolObservations',
    'if (event.type === "session.execution.succeeded" || event.type === "session.execution.interrupted") {\n\t\t\t\t\t\tif (event.type === "session.execution.succeeded") await recoverFinishedGenerations(event.data.sessionID, event.created);\n\t\t\t\t\t\tlangfuse.endActiveToolObservations',
  );
  source = replaceOnce(source,
    "\t\t\t\t}\n\t\t\t} catch (error) {\n\t\t\t\tif (!abort.signal.aborted) console.error(`Langfuse event subscription failed: ${String(error)}`);\n\t\t\t}\n\t\t})();",
    `\t\t\t\t\t} catch { console.warn("[AI Observatory] A trace event could not be recorded."); }
\t\t\t\t}
\t\t\t} catch { if (!abort.signal.aborted) console.warn("[AI Observatory] Trace event stream disconnected; retrying."); }
\t\t\tif (!abort.signal.aborted) await pause();
\t\t\t}
\t\t})();`,
  );
  // The shared client holds observations from every location. Retire only
  // sessions owned by this location, allowing a brief reload grace period.
  source = replaceOnce(source,
    "\t\t\tlangfuse.endActiveToolObservations();\n\t\t\tlangfuse.endActiveGenerationSteps();\n\t\t\tlangfuse.endActiveTurnObservations();\n\t\t\tlangfuse.clearTraceState();\n",
    "",
  );
  source = replaceOnce(source,
    "\t\t\tawait Promise.all(registrations.map((registration) => registration.dispose()));\n\t\t\tawait events;\n\t\t\tawait flush();\n\t\t};\n\t}\n};",
    `\t\t\tawait Promise.allSettled(registrations.map((registration) => Promise.resolve().then(() => registration.dispose())));
\t\t\tawait events;
\t\t\tretireLocation();
\t\t\tawait flush();
\t\t};
\t\t} catch (error) {
\t\t\tabort.abort();
\t\t\tif (previousGeneration !== undefined) {
\t\t\t\tlocation.generation = previousGeneration;
\t\t\t\tif (wasRetired) retireLocation(previousGeneration);
\t\t\t} else retireLocation();
\t\t\tthrow error;
\t\t}
\t}
};`,
  );
  return source;
}

export async function verifyPrepared() {
  try {
    const entries = await Promise.all([...directories, runtime, v2, packageMetadata].map((path) => lstat(path)));
    if (entries.some((entry) => entry.isSymbolicLink())) return false;
    if (entries.slice(0, directories.length).some((entry) => !entry.isDirectory())) return false;
    if (entries.slice(directories.length).some((entry) => !entry.isFile())) return false;
    const actual = await Promise.all([runtime, v2, packageMetadata].map(async (path) => digest(await readFile(path))));
    return actual.every((value, index) => value === [...preparedHashes, packageHash][index]);
  } catch {
    return false;
  }
}

export async function prepare() {
  const paths = [runtime, v2];
  const patches = [patchRuntime, patchV2];
  const prepared = await Promise.all(paths.map(async (path, index) => {
    const source = await readFile(path);
    const hash = digest(source);
    if (hash === preparedHashes[index]) return { source, changed: false };
    if (hash !== originalHashes[index]) throw new Error("Unexpected pinned upstream bundle");
    const result = Buffer.from(patches[index](source.toString("utf8")));
    if (digest(result) !== preparedHashes[index]) throw new Error(`Unexpected prepared hash: ${digest(result)}`);
    return { source: result, changed: true };
  }));
  await Promise.all(paths.map((path, index) => prepared[index].changed ? writeFile(path, prepared[index].source) : undefined));
}

if (process.argv[1] && fileURLToPath(import.meta.url) === process.argv[1]) {
  await prepare();
}
