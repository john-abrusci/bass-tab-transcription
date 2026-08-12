/** @type {import('next').NextConfig} */
const nextConfig = {
  experimental: {
    serverActions: {
      // Uploads go through a route handler, not a server action, but keep the
      // limit consistent with MAX_UPLOAD_BYTES in app/api/transcribe/route.ts.
      bodySizeLimit: "24mb",
    },
  },
};

export default nextConfig;
