/** Build a small static site from an explicit allowlist of repository files. */
import { copyFile, lstat, mkdir, readFile, readdir, writeFile } from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { Marked, Renderer } from "marked";
import { safeInput } from "./input.mjs";

const siteDir = path.dirname(fileURLToPath(import.meta.url));
const root = path.dirname(siteDir);
const destination = path.resolve(process.env.SITE_OUTPUT || path.join(siteDir, "dist"));
const publicUrl = "https://dmoliveira.github.io/coding-agent-observability/";
const repository = "https://github.com/dmoliveira/coding-agent-observability";
const pages = [
  { slug: "guide", label: "Getting started", title: "Get started", description: "Install the local stack, connect OpenCode, and manage sharing and backups.", source: "README.md" },
  { slug: "opencode", label: "OpenCode tracing", title: "OpenCode tracing", description: "Understand the integration, its coverage, credentials, and opt-out.", source: "docs/opencode.md" },
  { slug: "agent-observability", label: "Agent observability", title: "Agent observability", description: "Explore measured dashboards, structural reports, coverage, and privacy limits.", source: "docs/agent-observability.md" },
  { slug: "security", label: "Security & privacy", title: "Security & privacy", description: "Know the boundaries around your traces, network access, and backups.", source: "docs/security.md" },
  { slug: "licenses", label: "Third-party notices", title: "Third-party notices", description: "Attribution and licenses for the projects this stack builds upon.", source: "THIRD_PARTY_NOTICES.md" },
];
const expectedFiles = new Set([
  "index.html", "assets/site.css", "assets/mark.svg", "assets/observatory-hero.png",
  "robots.txt", "sitemap.xml", ...pages.map(({ slug }) => `${slug}/index.html`),
]);
const expectedDirectories = new Set(["assets", ...pages.map(({ slug }) => slug)]);
const escape = (value) => String(value).replace(/[&<>"']/g, (char) => ({
  "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
})[char]);

async function checkOutput(directory, relative = "") {
  let entries;
  try {
    const info = await lstat(directory);
    if (!info.isDirectory() || info.isSymbolicLink()) throw new Error("Site output must be a real directory.");
    entries = await readdir(directory, { withFileTypes: true });
  } catch (error) {
    if (error.code === "ENOENT") return;
    throw error;
  }
  for (const entry of entries) {
    const child = relative ? `${relative}/${entry.name}` : entry.name;
    if (entry.isSymbolicLink()) throw new Error("Site output must not contain symlinks.");
    if (entry.isDirectory() && expectedDirectories.has(child)) {
      await checkOutput(path.join(directory, entry.name), child);
    } else if (!entry.isFile() || !expectedFiles.has(child)) {
      throw new Error("Site output contains an unexpected path; refusing to publish it.");
    }
  }
}

function rewriteLink(href) {
  const [pathname, fragment] = href.split(/#(.*)/s, 2);
  const targets = {
    "README.md": "../guide/",
    "docs/opencode.md": "../opencode/", "opencode.md": "../opencode/",
    "docs/agent-observability.md": "../agent-observability/", "agent-observability.md": "../agent-observability/", "../README.md": "../guide/",
    "docs/security.md": "../security/", "security.md": "../security/",
    "THIRD_PARTY_NOTICES.md": "../licenses/",
  };
  if (Object.hasOwn(targets, pathname)) return targets[pathname] + (fragment ? `#${fragment}` : "");
  if (/^(?:https?:\/\/|mailto:|#)/i.test(href)) return href;
  throw new Error("Documentation contains an unsupported link; add an explicit mapping.");
}

function renderMarkdown(source) {
  const toc = [];
  const seen = new Map();
  const renderer = new Renderer();
  renderer.heading = function ({ tokens, depth }) {
    if (depth === 1) return "";
    const inline = this.parser.parseInline(tokens);
    const plain = inline.replace(/<[^>]*>/g, "").replace(/&amp;/g, "&");
    const base = plain.toLowerCase().replace(/[^\p{L}\p{N}\s_-]/gu, "").trim().replace(/\s+/g, "-") || "section";
    const count = seen.get(base) || 0;
    seen.set(base, count + 1);
    const id = count ? `${base}-${count}` : base;
    if (depth <= 3) toc.push({ id, label: plain, depth });
    return `<h${depth} id="${escape(id)}">${inline}</h${depth}>\n`;
  };
  renderer.link = function ({ href, title, tokens }) {
    const text = this.parser.parseInline(tokens);
    const target = rewriteLink(href);
    return `<a href="${escape(target)}"${title ? ` title="${escape(title)}"` : ""}>${text}</a>`;
  };
  renderer.image = function ({ href, text }) {
    if (href === "assets/observatory-hero.png") return `<img src="../assets/observatory-hero.png" alt="${escape(text)}">`;
    throw new Error("Documentation contains an unsupported image; add an explicit mapping.");
  };
  renderer.html = function ({ text }) {
    return /^(?:<a id="[a-z0-9-]+">|<\/a>)\s*$/i.test(text) ? text : escape(text);
  };
  const parser = new Marked({ renderer, gfm: true });
  return { html: parser.parse(source), toc };
}

function pageTemplate(page, content, toc) {
  const nav = pages.map((entry) => `<a href="../${entry.slug}/"${entry.slug === page.slug ? ' aria-current="page"' : ""}>${escape(entry.label)}</a>`).join("");
  const contents = toc.map((entry) => `<a href="#${escape(entry.id)}">${escape(entry.label)}</a>`).join("");
  return `<!doctype html>
<html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="theme-color" content="#09111c">
<meta http-equiv="Content-security-Policy" content="default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'none'; base-uri 'none'; form-action 'none'">
<title>${escape(page.title)} — AI Observatory</title><meta name="description" content="${escape(page.description)}">
<link rel="canonical" href="${publicUrl}${page.slug}/"><link rel="icon" href="../assets/mark.svg" type="image/svg+xml"><link rel="stylesheet" href="../assets/site.css">
</head><body>
<a class="skip-link" href="#main">Skip to content</a>
<header class="site-header"><div class="shell header-inner">
<a class="brand" href="../" aria-label="AI Observatory home"><img src="../assets/mark.svg" alt="" width="34" height="34"><span>AI Observatory<small>PRIVATE TRACES. CLEARER AGENTS.</small></span></a>
<nav class="main-nav" aria-label="Main navigation"><a href="../#how-it-works">How it works</a><a href="../guide/">Documentation</a><a href="../security/">Security</a><a class="nav-github" href="${repository}">GitHub <span aria-hidden="true">↗</span></a></nav>
</div></header>
<main id="main"><div class="docs-top"><div class="shell docs-top-inner"><p class="eyebrow">AI OBSERVATORY / DOCUMENTATION</p><h1>${escape(page.title)}</h1><p>${escape(page.description)}</p></div></div>
<div class="shell docs-layout"><aside class="docs-sidebar"><p class="docs-sidebar-title">EXPLORE</p><nav aria-label="Documentation">${nav}</nav>${contents ? `<p class="docs-sidebar-title toc-title">ON THIS PAGE</p><nav class="toc" aria-label="On this page">${contents}</nav>` : ""}</aside>
<div class="docs-main"><div class="docs-source"><span>Built from the repository documentation</span><a href="${repository}/blob/main/${escape(page.source)}">View source ↗</a></div><article class="markdown">${content}</article></div></div></main>
<footer class="site-footer"><div class="shell footer-inner"><a class="footer-brand" href="../"><img src="../assets/mark.svg" alt="" width="28" height="28"> AI Observatory</a><p>Private traces. Clearer agents.</p><div><a href="../guide/">Docs</a><a href="../security/">Security</a><a href="../licenses/">Licenses</a><a href="${repository}">GitHub ↗</a></div></div></footer>
</body></html>\n`;
}

await checkOutput(destination);
await mkdir(destination, { recursive: true });
for (const directory of expectedDirectories) await mkdir(path.join(destination, directory), { recursive: true });
await copyFile(await safeInput(root, "site/index.html"), path.join(destination, "index.html"));
await copyFile(await safeInput(root, "site/assets/site.css"), path.join(destination, "assets/site.css"));
await copyFile(await safeInput(root, "site/assets/mark.svg"), path.join(destination, "assets/mark.svg"));
await copyFile(await safeInput(root, "assets/observatory-hero.png"), path.join(destination, "assets/observatory-hero.png"));
for (const page of pages) {
  let source = await readFile(await safeInput(root, page.source), "utf8");
  if (page.source === "README.md") {
    source = source.replace(/^!\[AI Observatory[^\n]*\n/, "").replace(/^!\[Local first\][^\n]*\n/m, "");
  }
  const { html, toc } = renderMarkdown(source);
  await writeFile(path.join(destination, page.slug, "index.html"), pageTemplate(page, html, toc));
}
await writeFile(path.join(destination, "robots.txt"), `User-agent: *\nAllow: /\nSitemap: ${publicUrl}sitemap.xml\n`);
await writeFile(path.join(destination, "sitemap.xml"), `<?xml version="1.0" encoding="UTF-8"?>\n<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">${["", ...pages.map(({ slug }) => `${slug}/`)].map((slug) => `<url><loc>${publicUrl}${slug}</loc></url>`).join("")}</urlset>\n`);
console.log(`Built ${expectedFiles.size} allowlisted site files.`);
