import type { NextConfig } from "next";

// The browser talks only to this origin; /api/* is proxied to the FastAPI backend so auth cookies
// stay first-party (SameSite=Strict) and no API secrets ever reach the client bundle.
const API_URL = process.env.API_INTERNAL_URL ?? "http://localhost:8000";

const securityHeaders = [
  { key: "X-Content-Type-Options", value: "nosniff" },
  { key: "X-Frame-Options", value: "DENY" },
  { key: "Referrer-Policy", value: "strict-origin-when-cross-origin" },
  { key: "Permissions-Policy", value: "camera=(), microphone=(), geolocation=()" },
  {
    key: "Content-Security-Policy",
    value:
      "default-src 'self'; script-src 'self' 'unsafe-inline' https://telegram.org; " +
      "frame-src https://oauth.telegram.org; img-src 'self' data: https://t.me https://*.telegram.org; " +
      "style-src 'self' 'unsafe-inline'; connect-src 'self'; frame-ancestors 'none'",
  },
];

const config: NextConfig = {
  output: "standalone",
  poweredByHeader: false,
  async rewrites() {
    return [{ source: "/api/:path*", destination: `${API_URL}/api/:path*` }];
  },
  async headers() {
    return [{ source: "/:path*", headers: securityHeaders }];
  },
};

export default config;
