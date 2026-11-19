import assert from "node:assert/strict";
import { chmod, cp, mkdir, mkdtemp, readFile, rename, rm, symlink, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { pathToFileURL } from "node:url";
import test from "node:test";
import { activate, readConfig, validateConfig } from "../index.js";
import { prepare, verifyPrepared } from "../prepare.js";

const config = { publicKey: "pk-lf-test", secretKey: "sk-lf-test", baseUrl: "http://127.0.0.1:3000" };
const envKeys = ["LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY", "LANGFUSE_BASE_URL", "LANGFUSE_BASEURL", "AI_OBSERVATORY_DISABLED"];

test("only explicit loopback and Tailscale HTTPS destinations are accepted", () => {
  assert.equal(validateConfig(config), true);
  for (const baseUrl of ["https://cloud.langfuse.com", "http://example.ts.net:1234", "http://127.0.0.1:3000/path", "http://user@127.0.0.1:3000", "https://example.ts.net:15443?x=1"]) {
    assert.equal(validateConfig({ ...config, baseUrl }), false, baseUrl);
  }
  assert.equal(validateConfig({ ...config, baseUrl: "https://node.test.ts.net:15443" }), true);
});

test("credential file and parent must be private and not symlinks", async (t) => {
  const root = await mkdtemp(join(tmpdir(), "aiobs-plugin-test-"));
  t.after(() => rm(root, { recursive: true, force: true }));
  const dir = join(root, "ai-observatory");
  await mkdir(dir, { mode: 0o700 });
  const file = join(dir, "opencode-global.json");
  await writeFile(file, JSON.stringify(config), { mode: 0o600 });
  assert.deepEqual(await readConfig(root), config);
  await chmod(file, 0o644);
  assert.equal(await readConfig(root), null);
  await rm(file);
  await symlink(join(root, "elsewhere"), file);
  assert.equal(await readConfig(root), null);
});

test("trusted credentials are passed privately without changing the process environment", async (t) => {
  const before = new Map(envKeys.map((key) => [key, process.env[key]]));
  t.after(() => {
    for (const [key, value] of before) {
      if (value === undefined) delete process.env[key];
      else process.env[key] = value;
    }
  });
  process.env.LANGFUSE_PUBLIC_KEY = "pk-lf-other";
  process.env.LANGFUSE_SECRET_KEY = "sk-lf-other";
  process.env.LANGFUSE_BASE_URL = "https://cloud.example.invalid";
  delete process.env.LANGFUSE_BASEURL;
  delete process.env.AI_OBSERVATORY_DISABLED;
  const context = { options: { tags: ["existing"] } };
  let activated = 0;
  await Promise.all([config, { ...config, baseUrl: "http://127.0.0.1:4321" }].map((item) => activate(context, {
    readConfig: async () => item,
    verifyPrepared: async () => true,
    loadPlugin: async () => ({ default: { setup: async (scoped) => {
      await Promise.resolve();
      activated++;
      assert.deepEqual(scoped.options.__aiObservatoryCredentials, item);
      assert.equal(scoped.options.tags, undefined);
      assert.deepEqual(context.options.tags, ["existing"]);
      assert.equal(context.options.__aiObservatoryCredentials, undefined);
      assert.equal(process.env.LANGFUSE_PUBLIC_KEY, "pk-lf-other");
      assert.equal(process.env.LANGFUSE_SECRET_KEY, "sk-lf-other");
      assert.equal(process.env.LANGFUSE_BASE_URL, "https://cloud.example.invalid");
    } } }),
  })));
  assert.equal(activated, 2);
  assert.equal(process.env.LANGFUSE_PUBLIC_KEY, "pk-lf-other");
  assert.equal(process.env.LANGFUSE_SECRET_KEY, "sk-lf-other");
  assert.equal(process.env.LANGFUSE_BASE_URL, "https://cloud.example.invalid");
  assert.equal(process.env.LANGFUSE_BASEURL, undefined);
});

test("disabled startup never reads credentials or loads the official plugin", async (t) => {
  const before = process.env.AI_OBSERVATORY_DISABLED;
  t.after(() => {
    if (before === undefined) delete process.env.AI_OBSERVATORY_DISABLED;
    else process.env.AI_OBSERVATORY_DISABLED = before;
  });
  process.env.AI_OBSERVATORY_DISABLED = "1";
  await activate({}, { readConfig: () => assert.fail("read credentials"), loadPlugin: () => assert.fail("loaded plugin") });
});

test("unprepared dependencies never load", async (t) => {
  const before = process.env.AI_OBSERVATORY_DISABLED;
  t.after(() => {
    if (before === undefined) delete process.env.AI_OBSERVATORY_DISABLED;
    else process.env.AI_OBSERVATORY_DISABLED = before;
  });
  delete process.env.AI_OBSERVATORY_DISABLED;
  await activate({}, { readConfig: async () => config, verifyPrepared: async () => false, loadPlugin: () => assert.fail("loaded plugin") });
});

test("a failed setup disposes registrations without leaking errors", async (t) => {
  const before = process.env.AI_OBSERVATORY_DISABLED;
  t.after(() => {
    if (before === undefined) delete process.env.AI_OBSERVATORY_DISABLED;
    else process.env.AI_OBSERVATORY_DISABLED = before;
  });
  delete process.env.AI_OBSERVATORY_DISABLED;
  let disposed = 0;
  let calls = 0;
  const domain = { hook: async () => {
    if (++calls === 2) throw new Error("secret synthetic detail");
    return { dispose: async () => { disposed++; } };
  } };
  await activate({ session: domain, tool: domain }, {
    readConfig: async () => config,
    verifyPrepared: async () => true,
    loadPlugin: async () => ({ default: { setup: async (scoped) => {
      await scoped.session.hook("prompt", () => {});
      await scoped.session.hook("context", () => {});
    } } }),
  });
  assert.equal(disposed, 1);
});

test("failed synchronous disposal leaves a guarded hook inert", async () => {
  const callbacks = new Map();
  let count = 0;
  let executed = 0;
  const domain = { hook: async (name, callback) => {
    if (++count === 3) throw new Error("synthetic setup failure");
    callbacks.set(name, callback);
    return { dispose() {
      if (name === "prompt") throw new Error("synthetic disposer failure");
      callbacks.delete(name);
    } };
  } };
  await activate({ session: domain, tool: domain }, {
    readConfig: async () => config,
    verifyPrepared: async () => true,
    loadPlugin: async () => ({ default: { setup: async (scoped) => {
      await scoped.session.hook("prompt", () => { executed++; });
      await scoped.session.hook("context", () => { executed++; });
      await scoped.tool.hook("execute.before", () => {});
    } } }),
  });
  assert.equal(callbacks.has("context"), false);
  callbacks.get("prompt")?.({ sessionID: "synthetic" });
  assert.equal(executed, 0);
});

test("network debugging disables the adapter before credentials are read", async (t) => {
  const before = process.env.NODE_DEBUG;
  t.after(() => {
    if (before === undefined) delete process.env.NODE_DEBUG;
    else process.env.NODE_DEBUG = before;
  });
  process.env.NODE_DEBUG = "http,other";
  await activate({}, { readConfig: () => assert.fail("read credentials") });
});

test("pinned package preparation is verified and idempotent", async () => {
  assert.equal(await verifyPrepared(), true);
  await prepare();
  assert.equal(await verifyPrepared(), true);
});

test("integrity verification rejects metadata and symlink tampering", async (t) => {
  const root = await mkdtemp(join(tmpdir(), "aiobs-integrity-test-"));
  t.after(() => rm(root, { recursive: true, force: true }));
  const original = new URL("../", import.meta.url);
  await cp(original, root, { recursive: true });
  const verifier = await import(pathToFileURL(join(root, "prepare.js")).href);
  assert.equal(await verifier.verifyPrepared(), true);
  const pkg = join(root, "node_modules", "@langfuse", "opencode-observability-plugin");
  const metadata = join(pkg, "package.json");
  const text = await readFile(metadata, "utf8");
  const changed = JSON.parse(text);
  changed.exports["./v2"].import = "./malicious.js";
  await writeFile(metadata, JSON.stringify(changed));
  assert.equal(await verifier.verifyPrepared(), false);
  await writeFile(metadata, text);
  const runtime = join(pkg, "dist", "runtime-ku15Ukfq.js");
  const moved = `${runtime}.saved`;
  await rename(runtime, moved);
  await symlink(moved, runtime);
  assert.equal(await verifier.verifyPrepared(), false);
});
