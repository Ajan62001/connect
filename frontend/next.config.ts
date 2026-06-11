import type { NextConfig } from "next";

// NOTE: the rewrite destination is baked in at BUILD time for `next build`.
// 8001 is this machine's connect backend port (8000 is taken by an unrelated app).
const BACKEND_URL = process.env.BACKEND_URL ?? "http://localhost:8001";

const nextConfig: NextConfig = {
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
