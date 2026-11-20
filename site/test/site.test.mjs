import assert from "node:assert/strict";
import { mkdtemp, mkdir, readFile, readdir, rm, stat, symlink, writeFile } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { spawnSync } from "node:child_process";
import { after, before, test } from "node:test";
import { safeInput } from "../input.mjs";

const site = path.dirname(path.dirname(fileURLToPath(import.meta.url)));
let temporary;
let output;

function build(target) {
  return spawnSync(process.execPath, ["build.mjs"], {
    cwd: site,
    env: { ...process.env, SITE_OUTPUT: target },
    encoding: "utf8",
  });
}

async function files(directory, prefix = "") {
  const result = [];
  for (const entry of await readdir(directory, { withFileTypes: true })) {
    const relative = prefix ? `${prefix}/${entry.name}` : entry.name;
    if (entry.isDirectory()) result.push(...await files(path.join(directory, entry.name), relative));
    else result.push(relative);
  }
  return result.sort();
}

before(async () => {
  temporary = await mkdtemp(path.join(os.tmpdir(), "ai-observatory-site-test-"));
  output = path.join(temporary, "site");
  const result = build(output);
  assert.equal(result.status, 0, result.stderr);
});

after(async () => {
  if (temporary) await rm(temporary, { recursive: true, force: true });
});

test("builds only the intended public files and is repeatable", async () => {
  assert.deepEqual(await files(output), [
    "assets/mark.svg", "assets/observatory-hero.png", "assets/site.css", "guide/index.html",
    "index.html", "licenses/index.html", "opencode/index.html", "robots.txt", "security/index.html", "sitemap.xml",
  ]);
  const before = await readFile(path.join(output, "guide/index.html"), "utf8");
  const result = build(output);
  assert.equal(result.status, 0, result.stderr);
  assert.equal(await readFile(path.join(output, "guide/index.html"), "utf8"), before);
});

test("renders source documentation and important privacy boundaries", async () => {
  const guide = await readFile(path.join(output, "guide/index.html"), "utf8");
  const opencode = await readFile(path.join(output, "opencode/index.html"), "utf8");
  const security = await readFile(path.join(output, "security/index.html"), "utf8");
  const licenses = await readFile(path.join(output, "licenses/index.html"), "utf8");
  assert.match(guide, /coding-agent-observability\.git/);
  assert.match(guide, /using the|using Docker Compose/i);
  assert.match(guide, /href="\.\.\/security\/#tailscale-recovery"/);
  assert.match(opencode, /AI_OBSERVATORY_DISABLED=1 opencode --standalone/);
  assert.match(opencode, /<a id="opencode-integration"><\/a>/);
  assert.doesNotMatch(opencode, /&lt;a id=/);
  assert.match(security, /prompts, previous context, model output, reasoning and tool data/);
  assert.match(security, /id="tailscale-recovery"/);
  assert.match(licenses, /Copyright \(c\) 2023-2026 ClickHouse, Inc\./);
  assert.match(licenses, /Copyright \(c\) 2026 Langfuse GmbH/);
});

test("pages have accessible landmarks and working local links and fragments", async () => {
  const htmlFiles = (await files(output)).filter((file) => file.endsWith(".html"));
  const base = "https://site.invalid/coding-agent-observability/";
  for (const file of htmlFiles) {
    const html = await readFile(path.join(output, file), "utf8");
    assert.match(html, /<html lang="en">/);
    assert.match(html, /name="viewport"/);
    assert.match(html, /class="skip-link" href="#main"/);
    assert.match(html, /<main id="main">/);
    assert.match(html, /aria-label="Main navigation"/);
    assert.match(html, /script-src 'none'/);
    assert.equal((html.match(/<h1\b/g) || []).length, 1, file);
    const current = new URL(file, base);
    for (const match of html.matchAll(/<(?:a|img|link)\b[^>]*\b(?:href|src)="([^"]+)"/g)) {
      const reference = match[1].replaceAll("&amp;", "&");
      const resolved = new URL(reference, current);
      if (resolved.origin !== current.origin) continue;
      assert.ok(resolved.pathname.startsWith("/coding-agent-observability/"), `${file}: ${reference}`);
      let relative = decodeURIComponent(resolved.pathname.slice("/coding-agent-observability/".length));
      if (!relative || relative.endsWith("/")) relative += "index.html";
      const target = path.join(output, relative);
      assert.ok((await stat(target)).isFile(), `${file}: ${reference}`);
      if (resolved.hash) {
        const targetHtml = await readFile(target, "utf8");
        const id = decodeURIComponent(resolved.hash.slice(1));
        assert.ok(targetHtml.includes(`id="${id}"`), `${file}: missing ${reference}`);
      }
    }
  }
});

test("does not require third-party fonts, scripts, or analytics", async () => {
  const css = await readFile(path.join(output, "assets/site.css"), "utf8");
  assert.doesNotMatch(css, /@import|url\(["']?https?:/i);
  for (const file of (await files(output)).filter((name) => name.endsWith(".html"))) {
    const html = await readFile(path.join(output, file), "utf8");
    assert.doesNotMatch(html, /<script\b/i);
    assert.doesNotMatch(html, /<(?:img|script)\b[^>]*\bsrc="https?:/i);
    assert.doesNotMatch(html, /<link\b[^>]*rel="stylesheet"[^>]*href="https?:/i);
  }
});

test("refuses unexpected output files without removing or exposing them", async () => {
  const target = path.join(temporary, "unexpected");
  await mkdir(target);
  const sentinel = "synthetic_sensitive_value_123";
  await writeFile(path.join(target, ".env"), sentinel);
  const result = build(target);
  assert.notEqual(result.status, 0);
  assert.match(result.stderr, /unexpected path/);
  assert.doesNotMatch(result.stdout + result.stderr, new RegExp(sentinel));
  assert.equal(await readFile(path.join(target, ".env"), "utf8"), sentinel);
  assert.deepEqual(await files(target), [".env"]);
});

test("refuses symlinks inside the site output", async () => {
  const target = path.join(temporary, "symlinked");
  await mkdir(target);
  await symlink(temporary, path.join(target, "assets"));
  const result = build(target);
  assert.notEqual(result.status, 0);
  assert.match(result.stderr, /must not contain symlinks/);
});

test("refuses symlinked or escaping public-site input paths", async () => {
  const inputRoot = path.join(temporary, "inputs");
  await mkdir(inputRoot);
  await writeFile(path.join(inputRoot, "safe.md"), "public content");
  await symlink(path.join(inputRoot, "safe.md"), path.join(inputRoot, "linked.md"));
  assert.equal(await safeInput(inputRoot, "safe.md"), path.join(inputRoot, "safe.md"));
  await assert.rejects(safeInput(inputRoot, "linked.md"), /must not contain symlinks/);
  await assert.rejects(safeInput(inputRoot, "../safe.md"), /relative repository path/);
});
