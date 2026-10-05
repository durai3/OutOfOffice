import path from "node:path";
import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  reactStrictMode: true,
  // Pin the workspace root to web/ so a stray lockfile higher up the tree is ignored.
  turbopack: { root: path.resolve(__dirname) },
  // Keep the dev-tools button away from the sidebar footer.
  devIndicators: { position: "bottom-right" },
};

export default nextConfig;
