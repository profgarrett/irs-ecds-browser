#!/usr/bin/env node
// Local preview server for the static site in docs/ (stand-in for GitHub Pages).
//
//   node serve.js            -> http://127.0.0.1:8080/
//   node serve.js 3000       -> another port (or PORT=3000 node serve.js)
//   HOST=0.0.0.0 node serve.js   -> reachable from other devices on your network
//
// No dependencies (Node 18+). Files are sent with "Cache-Control: no-cache" plus an
// ETag, so edits show up on reload while unchanged files (e.g. data/names.json)
// come back as a quick 304 instead of being re-sent.

"use strict";
const http = require("http");
const fs = require("fs");
const path = require("path");

const ROOT = path.join(__dirname, "docs");
const PORT = Number(process.argv[2] || process.env.PORT || 8080);
const HOST = process.env.HOST || "127.0.0.1";

const TYPES = {
  ".html": "text/html; charset=utf-8",
  ".css": "text/css; charset=utf-8",
  ".js": "text/javascript; charset=utf-8",
  ".mjs": "text/javascript; charset=utf-8",
  ".json": "application/json; charset=utf-8",
  ".geojson": "application/geo+json",
  ".map": "application/json; charset=utf-8",
  ".txt": "text/plain; charset=utf-8",
  ".md": "text/markdown; charset=utf-8",
  ".csv": "text/csv; charset=utf-8",
  ".svg": "image/svg+xml",
  ".png": "image/png",
  ".jpg": "image/jpeg",
  ".jpeg": "image/jpeg",
  ".gif": "image/gif",
  ".webp": "image/webp",
  ".ico": "image/x-icon",
  ".woff": "font/woff",
  ".woff2": "font/woff2",
  ".pbf": "application/x-protobuf",
  ".pmtiles": "application/octet-stream",
};

function send(res, status, body, headers = {}) {
  res.writeHead(status, { "Content-Type": "text/plain; charset=utf-8", ...headers });
  res.end(body);
}

async function handle(req, res) {
  if (req.method !== "GET" && req.method !== "HEAD") {
    return send(res, 405, "Method Not Allowed\n", { Allow: "GET, HEAD" });
  }

  const { pathname, search } = new URL(req.url, "http://localhost");
  let urlPath;
  try {
    urlPath = decodeURIComponent(pathname);
  } catch {
    return send(res, 400, "Bad Request\n");
  }
  if (urlPath.includes("\0")) return send(res, 400, "Bad Request\n");

  // Resolve inside docs/ only.
  let file = path.join(ROOT, urlPath);
  if (file !== ROOT && !file.startsWith(ROOT + path.sep)) return send(res, 403, "Forbidden\n");

  let st = await fs.promises.stat(file).catch(() => null);
  if (st && st.isDirectory()) {
    if (!pathname.endsWith("/")) return send(res, 301, "", { Location: pathname + "/" + search });
    file = path.join(file, "index.html");
    st = await fs.promises.stat(file).catch(() => null);
  }
  if (!st || !st.isFile()) return send(res, 404, `Not Found: ${urlPath}\n`);

  const etag = `W/"${st.size.toString(16)}-${Math.floor(st.mtimeMs).toString(16)}"`;
  const headers = {
    "Content-Type": TYPES[path.extname(file).toLowerCase()] || "application/octet-stream",
    "Cache-Control": "no-cache",
    "Last-Modified": st.mtime.toUTCString(),
    ETag: etag,
  };
  if (req.headers["if-none-match"] === etag) {
    res.writeHead(304, headers);
    return res.end();
  }

  headers["Content-Length"] = st.size;
  res.writeHead(200, headers);
  if (req.method === "HEAD") return res.end();
  fs.createReadStream(file).on("error", () => res.destroy()).pipe(res);
}

const server = http.createServer((req, res) => {
  const t0 = Date.now();
  res.on("finish", () => console.log(`${res.statusCode} ${req.method} ${req.url} ${Date.now() - t0}ms`));
  handle(req, res).catch((err) => {
    console.error(err);
    if (!res.headersSent) send(res, 500, "Internal Server Error\n");
    else res.destroy();
  });
});

server.on("error", (err) => {
  if (err.code === "EADDRINUSE") console.error(`Port ${PORT} is already in use. Try: node serve.js ${PORT + 1}`);
  else console.error(err);
  process.exit(1);
});

server.listen(PORT, HOST, () => {
  const shown = HOST === "0.0.0.0" || HOST === "::" ? "localhost" : HOST;
  console.log(`Serving ${ROOT}\n  http://${shown}:${PORT}/\nPress Ctrl+C to stop.`);
});
