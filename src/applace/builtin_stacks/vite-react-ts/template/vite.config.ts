import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'

// Where this app's declared APIs are reached while developing. Applace sets
// these two when it starts the dev server; without them there is simply no
// proxy, and `npm run dev` by hand still works. Neither value carries the
// `VITE_` prefix, so neither can be inlined into anything a browser downloads
// -- they are read here, in node, and go no further.
const gateway = process.env.APPLACE_GATEWAY
const gatewayKey = process.env.APPLACE_GATEWAY_KEY ?? ''

export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: gateway
    ? {
        proxy: {
          '/api/gateway': {
            target: gateway,
            changeOrigin: true,
            headers: { 'x-applace-key': gatewayKey },
            rewrite: (path) => path.replace(/^\/api\/gateway/, ''),
          },
        },
      }
    : undefined,
})
