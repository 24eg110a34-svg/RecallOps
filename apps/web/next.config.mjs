/** @type {import('next').NextConfig} */

// Next 16 blocks dev resource requests (HMR, RSC payloads) from origins that are
// not listed, which breaks opening the console from another device on the LAN
// even though the app itself loads. Set NEXT_PUBLIC_DEV_ORIGINS to add your own.
const devOrigins = [
  "localhost",
  "127.0.0.1",
  ...(process.env.NEXT_PUBLIC_DEV_ORIGINS ?? "").split(",").map((o) => o.trim()).filter(Boolean),
];

const nextConfig = {
  reactStrictMode: true,
  allowedDevOrigins: devOrigins,
  env: {
    // Only forward the variable when it is genuinely set. Injecting a
    // 127.0.0.1 fallback here would defeat the client-side host detection in
    // src/lib/api.ts, which needs to stay undefined locally so the browser can
    // call the API on the same host the page is served from (the session is a
    // host-only cookie, so a mismatch logs the operator straight back out).
    ...(process.env.NEXT_PUBLIC_API_URL
      ? { NEXT_PUBLIC_API_URL: process.env.NEXT_PUBLIC_API_URL }
      : {}),
  },
};

export default nextConfig;
