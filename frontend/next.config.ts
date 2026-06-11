import type { NextConfig } from "next";

// Dev-only proxy: with `next dev` the rewrite below makes the frontend and
// backend one origin (no CORS). 8001 is this machine's connect backend port
// (8000 is taken by an unrelated app).
//
// IN PRODUCTION THE REWRITE IS OUT OF THE REQUEST PATH ENTIRELY: Caddy
// routes /api/* straight to the api container and everything else here
// (see /Caddyfile), so the browser's same-origin /api calls never touch
// Next. That structurally retires the old footgun where the rewrite
// destination was baked in at `next build` time (v02-runtime-deploy.md §7).
const BACKEND_URL = process.env.BACKEND_URL ?? "http://localhost:8001";

const nextConfig: NextConfig = {
  // Self-contained server bundle (.next/standalone) — the Dockerfile copies
  // it plus public/ and .next/static and runs `node server.js`; no
  // node_modules in the runtime image.
  output: "standalone",
  async rewrites() {
    return [
      {
        source: "/api/:path*",
        destination: `${BACKEND_URL}/api/:path*`,
      },
    ];
  },
};

export default nextConfig;
