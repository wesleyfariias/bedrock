import type { NextConfig } from "next";

const API = process.env.NEXT_PUBLIC_API ?? "http://127.0.0.1:8081";

const nextConfig: NextConfig = {
  async rewrites() {
    return [
      // envia /api/... -> FastAPI
      { source: "/api/:path*", destination: `${API}/:path*` },
    ];
  },
};

export default nextConfig;
