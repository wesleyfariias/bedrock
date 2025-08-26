import type { NextConfig } from "next";

const nextConfig: NextConfig = {
 
  async rewrites() {
    return [
     
      { source: "/api/chat", destination: "http://127.0.0.1:8081/chat" },
    ];
  },
};

export default nextConfig;
