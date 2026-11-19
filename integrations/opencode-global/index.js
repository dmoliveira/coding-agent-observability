/** Default-on, private-destination adapter for the official Langfuse plugin. */
import { constants } from "node:fs";
import { lstat, open } from "node:fs/promises";
import { homedir } from "node:os";
import { isAbsolute, join } from "node:path";
import { pluginUrl, verifyPrepared } from "./prepare.js";

const configName = "opencode-global.json";

export function validateConfig(value) {
  if (!value || typeof value !== "object" || Array.isArray(value)) return false;
  if (!/^pk-lf-[A-Za-z0-9_-]+$/.test(value.publicKey ?? "")) return false;
  if (!/^sk-lf-[A-Za-z0-9_-]+$/.test(value.secretKey ?? "")) return false;
  if (typeof value.baseUrl !== "string") return false;
  let url;
  try {
    url = new URL(value.baseUrl);
  } catch {
    return false;
  }
  if (url.username || url.password || url.pathname !== "/" || url.search || url.hash) return false;
  if (!url.port || value.baseUrl !== url.origin) return false;
  return (
    (url.protocol === "http:" && url.hostname === "127.0.0.1") ||
    (url.protocol === "https:" && url.hostname.endsWith(".ts.net"))
  );
}

export async function readConfig(root = process.env.XDG_CONFIG_HOME || join(homedir(), ".config")) {
  if (!isAbsolute(root)) return null;
  const directory = join(root, "ai-observatory");
  let file;
  try {
    const parent = await lstat(directory);
    if (!parent.isDirectory() || parent.isSymbolicLink() || parent.uid !== process.getuid() || parent.mode & 0o077) return null;
    file = await open(join(directory, configName), constants.O_RDONLY | (constants.O_NOFOLLOW || 0));
    const stat = await file.stat();
    if (!stat.isFile() || stat.uid !== process.getuid() || stat.mode & 0o077) return null;
    const value = JSON.parse(await file.readFile({ encoding: "utf8" }));
    return validateConfig(value) ? value : null;
  } catch {
    return null;
  } finally {
    await file?.close();
  }
}

export async function activate(ctx, options = {}) {
  if (process.env.AI_OBSERVATORY_DISABLED === "1") return;
  if (/(http|net|tls|undici|\*)/i.test(`${process.env.NODE_DEBUG ?? ""},${process.env.NODE_DEBUG_NATIVE ?? ""}`)) {
    console.warn("[AI Observatory] Tracing disabled while network debug logging is enabled.");
    return;
  }
  const registrations = [];
  let enabled = true;
  try {
    const config = await (options.readConfig ?? readConfig)();
    if (!validateConfig(config)) {
      console.warn("[AI Observatory] Tracing disabled: private credentials are missing or unsafe.");
      return;
    }
    if (!(await (options.verifyPrepared ?? verifyPrepared)())) {
      console.warn("[AI Observatory] Tracing disabled: the pinned plugin is not safely prepared.");
      return;
    }
    const plugin = await (options.loadPlugin ?? (() => import(pluginUrl.href)))();
    const credentials = Object.freeze({
      publicKey: config.publicKey,
      secretKey: config.secretKey,
      baseUrl: config.baseUrl,
    });
    // Pass credentials to our integrity-checked upstream instance only; do not
    // alter process.env or the options seen by other plugins.
    const scopedDomain = (domain) => new Proxy(domain, {
      get(target, key, receiver) {
        if (key === "hook") return async (name, callback) => {
          const guarded = function (...args) {
            return enabled ? Reflect.apply(callback, this, args) : undefined;
          };
          const registration = await Reflect.apply(target.hook, target, [name, guarded]);
          registrations.push(registration);
          return registration;
        };
        return Reflect.get(target, key, receiver);
      },
    });
    const scopedContext = new Proxy(ctx, {
      get(target, key, receiver) {
        if (key === "options") return { __aiObservatoryCredentials: credentials };
        if (key === "session" || key === "tool") return scopedDomain(target[key]);
        return Reflect.get(target, key, receiver);
      },
    });
    const cleanup = await plugin.default.setup(scopedContext);
    if (typeof cleanup !== "function") {
      enabled = false;
      await Promise.allSettled(registrations.map((registration) => Promise.resolve().then(() => registration.dispose())));
      return;
    }
    return async () => {
      enabled = false;
      await cleanup();
    };
  } catch {
    enabled = false;
    await Promise.allSettled(registrations.map((registration) => Promise.resolve().then(() => registration.dispose())));
    // Never print inherited environment values, credentials or plugin error payloads.
    console.warn("[AI Observatory] Tracing could not start; check the local server and plugin configuration.");
  }
}

export default { id: "ai-observatory.langfuse", setup: activate };
