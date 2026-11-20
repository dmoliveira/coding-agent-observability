"use strict";

// A fixed-destination, non-logging proxy. No host ports are published.
const http = require("node:http");

const MAX_BODY_BYTES = 32 * 1024 * 1024;
const UPSTREAM_HOST = "host.docker.internal";
const UPSTREAM_PORT = 1234;
const LISTEN_PORT = 8787;

function createGateway(host = UPSTREAM_HOST, port = UPSTREAM_PORT) {
  return http.createServer((request, response) => {
    if (request.method === "GET" && request.url === "/healthz") {
      request.resume();
      response.writeHead(204).end();
      return;
    }
    const permitted =
      (request.method === "GET" && request.url === "/v1/models") ||
      (request.method === "POST" && request.url === "/v1/chat/completions");
    if (!permitted) {
      request.resume();
      response.writeHead(404).end();
      return;
    }

    const length = request.headers["content-length"];
    if (length !== undefined && (!/^\d+$/.test(length) || Number(length) > MAX_BODY_BYTES)) {
      request.resume();
      response.writeHead(413).end();
      return;
    }
    const headers = {};
    for (const name of ["authorization", "content-type", "accept", "content-length"]) {
      if (typeof request.headers[name] === "string") headers[name] = request.headers[name];
    }
    const upstream = http.request({ hostname: host, port, method: request.method, path: request.url, headers, timeout: 120000 }, (result) => {
      // Never allow a provider redirect to turn this fixed proxy into a new destination.
      if (result.statusCode >= 300 && result.statusCode < 400) {
        result.resume();
        response.writeHead(502).end();
        return;
      }
      const safeHeaders = {};
      for (const name of ["content-type", "content-length", "retry-after"]) {
        if (typeof result.headers[name] === "string") safeHeaders[name] = result.headers[name];
      }
      response.writeHead(result.statusCode, safeHeaders);
      result.pipe(response);
    });
    upstream.on("timeout", () => upstream.destroy());
    upstream.on("error", () => {
      if (!response.headersSent) response.writeHead(502).end();
      else response.destroy();
    });
    request.on("aborted", () => upstream.destroy());
    response.on("close", () => { if (!response.writableEnded) upstream.destroy(); });
    let size = 0;
    request.on("data", (chunk) => {
      size += chunk.length;
      if (size > MAX_BODY_BYTES) {
        request.unpipe(upstream);
        upstream.destroy();
        if (!response.headersSent) response.writeHead(413).end();
        request.resume();
      }
    });
    request.pipe(upstream);
  });
}

if (require.main === module) createGateway().listen(LISTEN_PORT, "0.0.0.0");
module.exports = { createGateway };
